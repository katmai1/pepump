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


def test_log_dip_wait_progress_calcula_el_porcentaje_que_falta(caplog):
    """El helper de log periódico (_log_dip_wait_progress) tiene que
    mostrar cuánto falta bajar DESDE el precio actual para tocar el
    objetivo -no desde la referencia original."""
    import logging
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=5.0)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    bot.latest_price = 0.98  # precio actual mientras se espera
    target_price = 0.95      # objetivo (5% de baja desde una referencia de 1.0)

    with caplog.at_level(logging.INFO):
        bot._log_dip_wait_progress(target_price)

    messages = [r.message for r in caplog.records]
    assert any("Esperando la baja de entrada" in m and "falta bajar" in m for m in messages)
    # (0.98 - 0.95) / 0.98 * 100 = 3.06122...%
    assert any("3.06%" in m for m in messages)


def test_log_dip_wait_progress_no_loguea_si_ya_se_llego_al_objetivo(caplog):
    """Salvaguarda: si por alguna razón se llama con el objetivo ya
    alcanzado/superado, no debe mostrar un porcentaje negativo."""
    import logging
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=5.0)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    bot.latest_price = 0.90
    target_price = 0.95

    with caplog.at_level(logging.INFO):
        bot._log_dip_wait_progress(target_price)

    assert not any("falta bajar" in r.message for r in caplog.records)


def test_entry_dip_pct_loguea_progreso_periodicamente_end_to_end(caplog):
    """Extremo a extremo con un delay real entre eventos: el log
    periódico de progreso (tarea de fondo aparte, ver _dip_progress_logger)
    tiene que dispararse SIN cancelar ni romper el stream de eventos -la
    compra debe seguir concretándose normalmente después."""
    import logging

    class SlowFakeClient(FakeTradeStreamClient):
        async def iter_trade_events(self, ws):
            idx = self.sockets.index(ws)
            batch = self.events_by_connection[idx] if idx < len(self.events_by_connection) else []
            for i, event in enumerate(batch):
                if i > 0:
                    await asyncio.sleep(0.05)  # da tiempo a que el log periódico se dispare
                yield event

    client = SlowFakeClient(events_by_connection=[[
        {"price": 1.0},   # referencia
        {"price": 0.94},  # llega ~50ms después
    ]])
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=5.0, status_interval_seconds=0.01)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    async def scenario():
        bot._ws = await client.connect_trade_stream(cfg.mint)
        bot._trade_events = client.iter_trade_events(bot._ws)
        return await bot._get_initial_price()

    with caplog.at_level(logging.INFO):
        price = asyncio.run(scenario())

    assert price == 0.94
    # No debe haber intentado reconectar (el generador nunca se cierra
    # por el log periódico, que corre en una tarea aparte).
    assert client.connect_calls == 1
    assert any("Esperando la baja de entrada" in r.message and "falta bajar" in r.message
               for r in caplog.records)
