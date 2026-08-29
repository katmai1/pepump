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

    asyncio.run(asyncio.wait_for(bot._consume_trade_stream(), timeout=2.0))

    assert bot._using_onchain_fallback is True
    assert executor.sell_calls == 1
    assert bot.position.closed is True
    assert bot.latest_price == pytest.approx(1.10)


def test_handle_feed_stall_sin_pool_no_migra(monkeypatch):
    """Si _handle_feed_stall NO encuentra un pool de PumpSwap con precio
    (probablemente solo una pausa de volumen, no una migración real), no
    debe activar el fallback on-chain: el llamador tiene que seguir
    esperando el feed en vivo normalmente."""
    executor = SpyExecutor()
    cfg = make_config(stall_timeout_seconds=0.05)
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [None])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)

    result = asyncio.run(bot._handle_feed_stall())

    assert result is False
    assert bot._using_onchain_fallback is False
    assert bot.position.closed is False
    assert executor.sell_calls == 0
