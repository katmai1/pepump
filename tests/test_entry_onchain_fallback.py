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
import time

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


class FakeCurveOnChainClient:
    """Reemplaza PumpCurveOnChainClient. Devuelve (price, complete,
    exists) de una lista fija, uno por llamada (se queda en el último
    una vez agotada la lista)."""

    def __init__(self, rpc_url, results):
        self.rpc_url = rpc_url
        self._results = list(results)
        self.calls = 0

    async def fetch_price_or_status(self, mint: str):
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
    # La bonding curve TAMPOCO da nada legible acá (timeout de RPC,
    # exists=None -no una confirmación de que la cuenta no existe) -así
    # que el bot debe seguir esperando el feed en vivo igual que antes
    # de agregar el fallback de bonding curve.
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, [(None, False, None)])
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price == 1.23
    assert bot._onchain_source is None
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
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, [(None, False, None)])  # timeout de RPC, no confirmado
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price is None
    assert bot._onchain_source is None
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
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, [(None, False, None)])
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price is None
    assert bot._onchain_source is None
    # 2 llamadas, no 1: una es el chequeo temprano de existencia que
    # corre en paralelo desde el arranque (_confirm_mint_not_pumpfun,
    # ver _get_reference_price), la otra es el fallback "oficial" tras
    # el timeout del feed en vivo. Ninguna es un reintento del mismo
    # fallback -eso lo sigue cubriendo el assert de más abajo.
    assert fake_onchain.calls == 2
    assert fake_curve.calls == 0  # pool_confirmed_absent=False -> ni se prueba la bonding curve


def test_pool_encontrado_con_precio_usa_fallback_onchain(monkeypatch):
    """Caso de migración real: el fallback encuentra un pool con precio
    válido -ahí sí corresponde usar ese precio y marcar
    _onchain_source = "pumpswap" (comportamiento sin cambios)."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(live_feed_timeout_seconds=0.03, entry_wait_timeout_seconds=5.0)
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(0.0042, False)])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price == 0.0042
    assert bot._onchain_source == "pumpswap"
    # 2 llamadas: el chequeo temprano de existencia en paralelo (ver
    # _confirm_mint_not_pumpfun) más el fallback oficial que sí terminó
    # usando el precio.
    assert fake_onchain.calls == 2


def test_sin_pool_pero_bonding_curve_activa_usa_precio_de_bonding_curve(monkeypatch):
    """Caso NUEVO: no hay pool de PumpSwap (mint sigue en bonding curve)
    pero subscribeTokenTrade no está entregando trades reales. En vez de
    solo confirmar 'sin pool' y volver a esperar a ciegas el mismo feed
    que no está funcionando, el bot debe leer el precio DIRECTO de la
    cuenta de la bonding curve on-chain y usarlo como referencia,
    marcando _onchain_source = 'bondingcurve'."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(live_feed_timeout_seconds=0.03, entry_wait_timeout_seconds=5.0)
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(None, True)])  # sin pool -> sigue en bonding curve
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, [(0.0000000279, False, True)])
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price == pytest.approx(0.0000000279)
    assert bot._onchain_source == "bondingcurve"
    # 2 llamadas: el chequeo temprano de existencia en paralelo (ver
    # _confirm_mint_not_pumpfun) más el fallback oficial que sí terminó
    # usando el precio de la bonding curve.
    assert fake_curve.calls == 2


def test_mint_sin_bonding_curve_ni_pool_aborta_de_una(monkeypatch):
    """Caso NUEVO: ni el pool de PumpSwap ni la cuenta de bonding curve
    existen para este mint, y AMBAS consultas confirmaron la ausencia
    (no fallaron por timeout de RPC) -> el mint nunca se lanzó en
    pump.fun (ej. un mint nativo de Raydium/Meteora/otro DEX). El bot
    debe abortar de una, sin esperar entry_wait_timeout_seconds ni
    reintentar el feed en vivo, porque acá no hay ninguna fuente de
    precio que vaya a aparecer."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(
        live_feed_timeout_seconds=0.03,
        entry_wait_timeout_seconds=5.0,  # deliberadamente grande: no debería llegar a usarse
    )
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(None, True)])  # sin pool, confirmado
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, [(None, False, False)])  # sin cuenta, confirmado
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price is None
    assert bot._onchain_source is None
    assert fake_onchain.calls == 1  # ni un solo reintento
    assert fake_curve.calls == 1


class FakeCpmmOnChainClient:
    """Reemplaza RaydiumCpmmOnChainClient. Devuelve (price,
    confirmed_absent) de una lista fija, uno por llamada."""

    def __init__(self, rpc_url, results):
        self.rpc_url = rpc_url
        self._results = list(results)
        self.calls = 0

    async def fetch_price_or_confirm_absent(self, mint: str):
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result

    async def fetch_price(self, mint: str):
        price, _ = await self.fetch_price_or_confirm_absent(mint)
        return price


def test_mint_de_raydium_cpmm_usa_precio_del_pool_y_rutea_por_raydium_cpmm(monkeypatch):
    """Caso real (Dz9mQ9...bonk, bonk.fun graduado a Raydium CPMM): ni
    bonding curve ni pool oficial de PumpSwap, pero sí un pool CPMM contra
    SOL. Antes el bot abortaba (o, peor, usaba un pool de relleno de
    PumpSwap y forzaba pool="pump-amm" -> "Pool account not found")."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(live_feed_timeout_seconds=0.03, entry_wait_timeout_seconds=5.0)
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(None, True)])
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, [(None, False, False)])
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)
    fake_cpmm = FakeCpmmOnChainClient(cfg.solana_rpc_url, [(0.00206, False)])
    monkeypatch.setattr(bot_module, "RaydiumCpmmOnChainClient", lambda rpc_url: fake_cpmm)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price == pytest.approx(0.00206)
    assert bot._onchain_source == "raydium-cpmm"
    assert bot._current_pool_override() == "raydium-cpmm"
    # El polling de la posición también sale del pool CPMM.
    assert asyncio.run(bot._onchain_fetch_price()) == pytest.approx(0.00206)


def test_mint_sin_bonding_curve_ni_pool_aborta_sin_esperar_live_feed_timeout(monkeypatch):
    """Regresión del bug reportado: antes, aunque el mint NUNCA hubiera
    sido de pump.fun, el bot se quedaba esperando `live_feed_timeout_seconds`
    (más el tiempo de la consulta on-chain) antes de siquiera intentar
    confirmarlo -tarde para algo que no depende de ningún timeout de
    volumen. Con el chequeo temprano en paralelo
    (_confirm_mint_not_pumpfun), la detección no debe depender de
    `live_feed_timeout_seconds`: acá lo dejamos deliberadamente grande y
    el bot igual debe abortar casi de inmediato."""
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(
        live_feed_timeout_seconds=5.0,  # deliberadamente grande
        entry_wait_timeout_seconds=10.0,
    )
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot._trade_events = _ack_then_hang()

    fake_onchain = FakeOnChainClient(cfg.solana_rpc_url, [(None, True)])  # sin pool, confirmado
    monkeypatch.setattr(bot_module, "PumpSwapOnChainClient", lambda rpc_url: fake_onchain)
    fake_curve = FakeCurveOnChainClient(cfg.solana_rpc_url, [(None, False, False)])  # sin cuenta, confirmado
    monkeypatch.setattr(bot_module, "PumpCurveOnChainClient", lambda rpc_url: fake_curve)

    start = time.monotonic()
    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))
    elapsed = time.monotonic() - start

    assert price is None
    assert bot._onchain_source is None
    assert elapsed < 1.0  # mucho antes de los 5s de live_feed_timeout_seconds
    assert fake_onchain.calls == 1
    assert fake_curve.calls == 1
