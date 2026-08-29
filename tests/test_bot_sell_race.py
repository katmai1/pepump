"""
Tests para la nueva opción `entry_dip_pct`: si es 0, comprar de una al
precio de referencia (comportamiento de siempre). Si es > 0, esperar a
que el precio baje ese %% desde la referencia antes de comprar.
"""
import asyncio

from pepump.bot import TrailingTakeProfitBot
from tests.conftest import FakeTradeStreamClient, SpyExecutor, make_config


def test_entry_dip_pct_zero_compra_en_la_referencia():
    """entry_dip_pct=0 (default) -> comportamiento sin cambios: compra
    con el primer precio real que llega."""
    client = FakeTradeStreamClient(events_by_connection=[[
        {"price": 1.0},
    ]])
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=0.0)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    async def scenario():
        bot._ws = await client.connect_trade_stream(cfg.mint)
        bot._trade_events = client.iter_trade_events(bot._ws)
        return await bot._get_initial_price()

    price = asyncio.run(scenario())
    assert price == 1.0


def test_entry_dip_pct_espera_la_baja_antes_de_entrar():
    """entry_dip_pct=5 -> la referencia (1.0) NO es el precio de compra;
    el bot debe esperar hasta ver un precio <= 0.95 (baja de 5%%)."""
    client = FakeTradeStreamClient(events_by_connection=[[
        {"price": 1.0},   # referencia
        {"price": 0.98},  # baja de 2%: todavía no alcanza
        {"price": 0.97},  # baja de 3%: todavía no alcanza
        {"price": 0.94},  # baja de 6%: ¡ya alcanzó/superó el 5%!
        {"price": 0.90},  # no debería llegar a consumirse
    ]])
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=5.0)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    async def scenario():
        bot._ws = await client.connect_trade_stream(cfg.mint)
        bot._trade_events = client.iter_trade_events(bot._ws)
        return await bot._get_initial_price()

    price = asyncio.run(scenario())
    assert price == 0.94


def test_entry_dip_pct_reconecta_si_se_corta_mientras_espera_la_baja():
    """Si la conexión se corta MIENTRAS se espera la baja (todavía sin
    posición abierta), el bot reconecta solo y sigue esperando -igual
    que ya hacía _consume_trade_stream una vez con posición abierta."""
    client = FakeTradeStreamClient(events_by_connection=[
        [{"price": 1.0}],       # conexión 1: llega la referencia y se corta
        [{"price": 0.9}],       # conexión 2 (reconectada): ya alcanza el 5% de baja
    ])
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=5.0)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    async def scenario():
        bot._ws = await client.connect_trade_stream(cfg.mint)
        bot._trade_events = client.iter_trade_events(bot._ws)
        return await bot._get_initial_price()

    price = asyncio.run(scenario())
    assert price == 0.9
    assert client.connect_calls == 2
