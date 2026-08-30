"""
Tests para el BUGFIX del precio de ENTRADA cuando el feed en vivo se
queda en silencio (`live_feed_timeout_seconds`) pero el fallback
on-chain NO encuentra ningún pool de PumpSwap para el mint.

Antes de este fix, "no se encontró pool" se trataba como sinónimo de
"no hay ninguna fuente de precio" y el bot abortaba la entrada de una.
Pero "no se encontró pool" en realidad CONFIRMA que el mint todavía no
migró -sigue en bonding curve-, así que el feed en vivo sigue siendo la
fuente correcta; el silencio inicial puede ser solo un mint con poco
volumen (ver el propio mensaje de log: "Esto puede tardar si el token
tiene poco volumen"). Ahora el bot vuelve a esperar el feed en vivo en
esos casos, con un límite total configurable (`entry_wait_timeout_seconds`)
para no esperar para siempre en un mint realmente sin actividad.
"""
import asyncio

import pytest

from pepump import bot as bot_module
from pepump.bot import TrailingTakeProfitBot
from tests.conftest import FakeTradeStreamClient, SpyExecutor, make_config


class FakeOnChainClient:
    """Reemplaza PumpSwapOnChainClient. Devuelve (price, pool_confirmed_absent)
    de una lista fija, uno por llamada (se queda en el último una vez
    agotada la lista)."""

    def __init__(self, rpc_url, results):
        self.rpc_url = rpc_url
        self._results = list(results)
        self.calls = 0

    async def fetch_price_or_confirm_absent(self, mint: str):
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


async def _ack_then_hang():
    """Simula: llega el ack de suscripción y después el feed se queda
    en silencio para siempre (mint sin volumen o ya migrado)."""
    yield {"message": "Successfully subscribed to keys."}
    await asyncio.Future()
    yield {}  # pragma: no cover - nunca se llega acá


async def _ack_then_hang_then_trade(delay: float, price: float):
    """Simula: llega el ack, silencio por `delay` segundos, y recién
    ahí un trade real -el caso típico de un mint de bajo volumen que
    finalmente opera, NO un mint migrado."""
    yield {"message": "Successfully subscribed to keys."}
    await asyncio.sleep(delay)
    yield {"price": price}


def test_sin_pool_confirmado_sigue_esperando_y_compra_con_trade_tardio(monkeypatch):
    """Si el fallback on-chain confirma repetidamente 'no hay pool', el
    bot NO debe abortar: debe seguir esperando el feed en vivo, y usar
    el precio del trade real en cuanto llega -sin haber marcado
    _using_onchain_fallback en ningún momento, porque el mint nunca
    migró."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(
        live_feed_timeout_seconds=0.05,
        entry_wait_timeout_seconds=2.0,
    )
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang_then_trade(delay=0.18, price=1.23)

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(None, True)])  # siempre "sin pool"
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price == 1.23
    assert bot._using_onchain_fallback is False
    assert fake_onchain.calls >= 2  # reintentó el fallback más de una vez antes de que llegara el trade


def test_sin_pool_confirmado_aborta_al_superar_entry_wait_timeout(monkeypatch):
    """Si nunca llega ni un trade real ni un pool, el bot debe abortar
    -pero recién después de agotar `entry_wait_timeout_seconds`, no en
    el primer intento de fallback."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(
        live_feed_timeout_seconds=0.03,
        entry_wait_timeout_seconds=0.15,
    )
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(None, True)])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price is None
    assert bot._using_onchain_fallback is False
    assert fake_onchain.calls >= 2  # reintentó varias veces antes de rendirse


def test_pool_encontrado_pero_sin_precio_aborta_sin_reintentar_feed(monkeypatch):
    """Si el fallback on-chain NO puede confirmar 'sin pool' (ej.
    encontró un pool pero no pudo leer el precio, o la consulta on-chain
    en sí falló), el bot debe abortar de una -no hay ninguna base para
    asumir que conviene seguir esperando el feed en vivo."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(
        live_feed_timeout_seconds=0.03,
        entry_wait_timeout_seconds=5.0,  # deliberadamente grande: no debería llegar a usarse
    )
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(None, False)])  # pool roto / consulta fallida
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price is None
    assert bot._using_onchain_fallback is False
    assert fake_onchain.calls == 1  # ni un solo reintento


def test_pool_encontrado_con_precio_usa_fallback_onchain(monkeypatch):
    """Caso de migración real: el fallback encuentra un pool con precio
    válido -ahí sí corresponde usar ese precio y marcar
    _using_onchain_fallback (comportamiento sin cambios)."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(live_feed_timeout_seconds=0.03, entry_wait_timeout_seconds=5.0)
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(0.0042, False)])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price == 0.0042
    assert bot._using_onchain_fallback is True
    assert fake_onchain.calls == 1
