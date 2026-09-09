"""
Tests para el BUGFIX de "precio congelado" cuando el mint migra de la
bonding curve de pump.fun a PumpSwap A MITAD de una posición ya abierta
(con el feed en vivo funcionando bien en el momento de la entrada).

Antes de este fix, subscribeTokenTrade simplemente dejaba de mandar
trades para el mint migrado sin ningún error ni cierre de socket, así
que _consume_trade_stream se quedaba esperando para siempre y el precio
quedaba pegado en el último valor -el status printer lo repetía sin
parar-. Ver _handle_feed_stall / stall_timeout_seconds en bot.py.
"""
import asyncio

import pytest

from pepump import bot as bot_module
from pepump.bot import TrailingTakeProfitBot
from pepump.executor import Position
from tests.conftest import SpyExecutor, make_config


async def _hanging_stream():
    """Async generator que nunca entrega ningún evento (simula el feed
    en vivo yéndose en silencio tras la migración: el socket sigue
    'abierto' pero no llega nada más)."""
    await asyncio.Future()
    yield {}  # pragma: no cover - nunca se llega acá


class FakeOnChainClient:
    """Reemplaza PumpSwapOnChainClient: devuelve precios de una lista
    fija, uno por llamada (se queda en el último una vez agotada)."""

    def __init__(self, rpc_url, prices):
        self.rpc_url = rpc_url
        self._prices = list(prices)
        self.calls = 0

    async def fetch_price_for_migrated_mint(self, mint: str):
        price = self._prices[min(self.calls, len(self._prices) - 1)]
        self.calls += 1
        return price


class FakeCurveOnChainClient:
    """Reemplaza PumpCurveOnChainClient en estos tests: devuelve
    (price, complete, exists) fijos, sin pegarle a la red."""

    def __init__(self, rpc_url, price=None, complete=False, exists=False):
        self.rpc_url = rpc_url
        self._price = price
        self._complete = complete
        self._exists = exists
        self.calls = 0

    async def fetch_price_or_status(self, mint: str):
        self.calls += 1
        return self._price, self._complete, self._exists


def test_migracion_a_mitad_de_posicion_pasa_a_polling_onchain(monkeypatch):
    """Si el feed en vivo deja de mandar trades DESPUÉS de haber
    comprado y hay un pool de PumpSwap con precio válido, el bot debe
    detectar el 'stall', confirmar la migración vía _handle_feed_stall
    y pasar a polling on-chain -sin quedarse con el precio congelado
    para siempre-, aplicando la lógica normal de trailing-stop sobre
    los precios que llegan por polling."""
    executor = SpyExecutor()
    cfg = make_config(
        stall_timeout_seconds=0.05,
        onchain_poll_interval_seconds=0.02,
        activation_pct=10.0,
        trailing_pct=15.0,
        initial_stop_pct=25.0,
    )
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)
    bot._trade_events = _hanging_stream()  # nunca entrega otro trade -> dispara el stall

    # 1.20 arma el trailing (>= +10% de entrada); 1.30 pone nuevo máximo
    # (stop en 1.30*0.85=1.105); 1.10 retrocede por debajo de ese stop -> vende.
    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [1.20, 1.30, 1.10])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)
    # No debería llegar a consultarse (el fake de PumpSwap ya da precio
    # válido de una), pero lo dejamos parcheado igual para no pegarle a
    # la red real si algo cambia.
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient",
                         lambda rpc_url: FakeCurveOnChainClient(rpc_url))

    asyncio.run(asyncio.wait_for(bot._consume_trade_stream(), timeout=2.0))

    assert bot._onchain_source == "pumpswap"
    assert executor.sell_calls == 1
    assert bot.position.closed is True
    assert bot.latest_price == pytest.approx(1.10)


def test_handle_feed_stall_sin_pool_ni_bonding_curve_no_migra(monkeypatch):
    """Si _handle_feed_stall NO encuentra ni un pool de PumpSwap ni un
    precio legible en la bonding curve (probablemente solo una pausa de
    volumen, no una migración real ni un problema puntual del feed), no
    debe activar ningún fallback on-chain: el llamador tiene que seguir
    esperando el feed en vivo normalmente."""
    executor = SpyExecutor()
    cfg = make_config(stall_timeout_seconds=0.05)
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [None])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, price=None, complete=False, exists=False)
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    result = asyncio.run(bot._handle_feed_stall())

    assert result is False
    assert bot._onchain_source is None
    assert bot.position.closed is False
    assert executor.sell_calls == 0
    assert bot.latest_price is None  # no se tocó: no había ningún precio legible


def test_handle_feed_stall_bonding_curve_da_precio_de_seguridad(monkeypatch):
    """Si no hay pool de PumpSwap pero la bonding curve SÍ responde con
    un precio válido (y no completada), _handle_feed_stall debe usarlo
    como red de seguridad -actualiza latest_price y dispara
    _on_price_update- pero SIN activar el modo polling on-chain: sigue
    devolviendo False para que _consume_trade_stream siga intentando
    recibir trades reales del feed en vivo."""
    executor = SpyExecutor()
    cfg = make_config(stall_timeout_seconds=0.05, activation_pct=10.0, trailing_pct=15.0, initial_stop_pct=25.0)
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [None])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, price=1.05, complete=False, exists=True)
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    result = asyncio.run(bot._handle_feed_stall())

    assert result is False
    assert bot._onchain_source is None  # NO pasa a modo polling on-chain
    assert bot.latest_price == pytest.approx(1.05)
    assert bot.position.closed is False  # 1.05 no dispara ni stop-loss ni trailing


# --------------------------------------------------------------------------- #
# Feed muerto para un mint que sigue en bonding curve
# --------------------------------------------------------------------------- #
#
# Un stall aislado es probablemente una pausa de volumen y no justifica
# abandonar el feed en vivo. Pero si se repiten, el trailing-stop pasa a
# evaluarse solo una vez cada `stall_timeout_seconds` (20s por defecto):
# demasiada ceguera con la posición abierta. A partir del segundo stall
# seguido se pasa a polling on-chain de la curva.

def test_stalls_repetidos_con_la_curva_viva_pasan_a_polling_onchain(monkeypatch):
    executor = SpyExecutor()
    cfg = make_config(
        stall_timeout_seconds=0.05,
        onchain_poll_interval_seconds=0.02,
        activation_pct=10.0,
        trailing_pct=15.0,
        initial_stop_pct=25.0,
    )
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    # Nunca hay pool de PumpSwap: el mint sigue en bonding curve.
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient",
                         lambda rpc_url: FakeOnChainClient(rpc_url, [None]))

    # La curva responde: 1.02 (primer stall, solo red de seguridad),
    # 1.20 (segundo stall -> pasa a polling y arma el trailing),
    # 1.30 (nuevo máximo, stop en 1.105), 0.90 (retrocede -> vende).
    precios = iter([1.02, 1.20, 1.30, 0.90])
    ultimo = [1.02]

    class CurvaConSecuencia(FakeCurveOnChainClient):
        async def fetch_price_or_status(self, mint):
            self.calls += 1
            ultimo[0] = next(precios, ultimo[0])
            return ultimo[0], False, True

    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient",
                         lambda rpc_url: CurvaConSecuencia(rpc_url))

    async def escenario():
        # Primer stall: red de seguridad, sigue esperando el feed en vivo.
        primero = await bot._handle_feed_stall()
        assert primero is False
        assert bot._onchain_source is None
        assert bot.latest_price == pytest.approx(1.02)

        # Segundo stall seguido: deja el feed y hace polling hasta cerrar.
        return await bot._handle_feed_stall()

    resultado = asyncio.run(asyncio.wait_for(escenario(), timeout=2.0))

    assert resultado is True
    assert bot._onchain_source == "bondingcurve"
    assert executor.sell_calls == 1
    assert bot.position.closed is True


def test_un_trade_real_reinicia_el_contador_de_stalls():
    """Si el feed vuelve a entregar, los stalls acumulados no cuentan:
    hacen falta otros dos seguidos para abandonarlo."""
    cfg = make_config(stall_timeout_seconds=0.05, activation_pct=10.0, initial_stop_pct=25.0)
    bot = TrailingTakeProfitBot(client=None, executor=SpyExecutor(), config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)
    bot._curve_stalls = 1

    async def un_evento():
        yield {"price": 1.01}

    bot._trade_events = un_evento()
    bot.client = type("C", (), {"extract_price": staticmethod(lambda e: e.get("price"))})()

    # El stream entrega un trade y después se agota (StopAsyncIteration ->
    # intento de reconexión, que falla porque no hay cliente real). Lo que
    # importa es que el evento bueno pasó por el reseteo del contador.
    async def correr():
        try:
            await asyncio.wait_for(bot._consume_trade_stream(), timeout=0.3)
        except Exception:
            pass

    asyncio.run(correr())

    assert bot._curve_stalls == 0
