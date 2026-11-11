"""
Tests para la nueva opción `entry_dip_pct`: si es 0, comprar de inmediato al
precio de referencia (comportamiento de siempre). Si es > 0, esperar a
que el precio baje ese %% desde la referencia antes de comprar.
"""
import asyncio
import logging

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
        await bot._stream.connect()
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
        await bot._stream.connect()
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
        await bot._stream.connect()
        return await bot._get_initial_price()

    price = asyncio.run(scenario())
    assert price == 0.9
    assert client.connect_calls == 2


def test_entry_dip_pct_reconecta_ante_un_error_de_red_no_limpio(caplog):
    """Igual que el test de arriba, pero cuando el feed se cae con un
    error de red de verdad (no un cierre limpio del server). Las dos
    ramas comparten el mismo `except Exception`, así que este test fija
    que un error crudo también reconecta -y que el log dice 'conexión
    interrumpida' con el error adentro, no 'la conexión se cerró'."""

    class BoomThenRecoverClient(FakeTradeStreamClient):
        """Primera conexión: entrega la referencia y después revienta con
        un error de red. Segunda: entrega el precio que dispara la compra."""

        async def iter_trade_events(self, ws):
            if self.sockets.index(ws) == 0:
                yield {"price": 1.0}
                raise ConnectionResetError("el socket se murió (fake)")
            yield {"price": 0.9}

    client = BoomThenRecoverClient()
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=5.0)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    async def scenario():
        await bot._stream.connect()
        return await bot._get_initial_price()

    with caplog.at_level(logging.WARNING):
        price = asyncio.run(scenario())

    assert price == 0.9
    assert client.connect_calls == 2
    mensajes = " | ".join(r.getMessage() for r in caplog.records)
    assert "conexión interrumpida" in mensajes
    assert "el socket se murió (fake)" in mensajes
    assert "la conexión se cerró" not in mensajes


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
        await bot._stream.connect()
        return await bot._get_initial_price()

    with caplog.at_level(logging.INFO):
        price = asyncio.run(scenario())

    assert price == 0.94
    # No debe haber intentado reconectar (el generador nunca se cierra
    # por el log periódico, que corre en una tarea aparte).
    assert client.connect_calls == 1
    assert any("Esperando la baja de entrada" in r.message and "falta bajar" in r.message
               for r in caplog.records)


# ---- Espera máxima de la baja (entry_dip_timeout_minutes) ---------------- #
# Caso real: con entry_dip_pct sin límite de espera, una posición podía
# quedarse días esperando una baja que no llegaba, ocupando un hueco de
# max_open_positions. Con las 49 posiciones reales del 31/10 al 11/11/2026,
# 6 h de espera daban el mejor resultado (ver config.py).


class _FeedQueSeCalla(FakeTradeStreamClient):
    """Entrega su tanda y luego no manda nada más, sin cerrar el socket:
    un token sin trades mientras se espera la baja."""

    async def iter_trade_events(self, ws):
        async for event in super().iter_trade_events(ws):
            yield event
        await asyncio.Event().wait()


def test_entry_dip_timeout_cancela_sin_comprar_si_el_feed_no_llega_a_la_baja():
    client = _FeedQueSeCalla(events_by_connection=[[{"price": 1.0}, {"price": 0.97}]])
    cfg = make_config(entry_dip_pct=5.0, entry_dip_timeout_minutes=0.002)  # 0,12 s
    bot = TrailingTakeProfitBot(client, SpyExecutor(), cfg)

    async def scenario():
        await bot._stream.connect()
        return await asyncio.wait_for(bot._get_initial_price(), timeout=5)

    assert asyncio.run(scenario()) is None
    assert "no llegó" in bot._entry_abort_reason
    assert "no se compró" in bot._entry_abort_reason
    assert bot.latest_price == 0.97


def test_entry_dip_timeout_deja_el_motivo_en_end_reason_y_no_compra():
    client = _FeedQueSeCalla(events_by_connection=[[{"price": 1.0}]])
    executor = SpyExecutor()
    cfg = make_config(entry_dip_pct=8.0, entry_dip_timeout_minutes=0.002)
    bot = TrailingTakeProfitBot(client, executor, cfg)

    async def scenario():
        await bot._stream.connect()
        return await asyncio.wait_for(bot._enter(), timeout=5)

    assert asyncio.run(scenario()) is False
    assert executor.buy_calls == 0
    assert bot.end_reason == "la baja de entrada del 8.0% no llegó en 0.002 min; no se compró"


def test_entry_dip_timeout_compra_si_la_baja_llega_antes_del_plazo():
    client = _FeedQueSeCalla(events_by_connection=[[{"price": 1.0}, {"price": 0.94}]])
    cfg = make_config(entry_dip_pct=5.0, entry_dip_timeout_minutes=0.002)
    bot = TrailingTakeProfitBot(client, SpyExecutor(), cfg)

    async def scenario():
        await bot._stream.connect()
        return await asyncio.wait_for(bot._get_initial_price(), timeout=5)

    assert asyncio.run(scenario()) == 0.94
    assert bot._entry_abort_reason is None


def test_entry_dip_timeout_cero_espera_sin_limite():
    """0 = sin límite: sigue esperando (lo corta el wait_for del test)."""
    client = _FeedQueSeCalla(events_by_connection=[[{"price": 1.0}]])
    cfg = make_config(entry_dip_pct=5.0, entry_dip_timeout_minutes=0.0)
    bot = TrailingTakeProfitBot(client, SpyExecutor(), cfg)

    async def scenario():
        await bot._stream.connect()
        await asyncio.wait_for(bot._get_initial_price(), timeout=0.3)

    import pytest
    with pytest.raises(TimeoutError):
        asyncio.run(scenario())
    assert bot._dip_deadline is None
    assert bot._entry_abort_reason is None


def test_entry_dip_timeout_cancela_tambien_con_precio_on_chain():
    """Rama on-chain (mint migrado): el precio llega en cada consulta pero
    nunca baja lo suficiente; el plazo corta aunque el polling sea más largo."""
    cfg = make_config(entry_dip_pct=5.0, entry_dip_timeout_minutes=0.002, onchain_poll_interval_seconds=30.0)
    bot = TrailingTakeProfitBot(None, SpyExecutor(), cfg)
    bot.onchain.source = "pumpswap"
    consultas = []

    async def fetch_price():
        consultas.append(1)
        return 0.99

    bot.onchain.fetch_price = fetch_price

    async def scenario():
        return await asyncio.wait_for(bot._wait_for_dip_entry(1.0, 0.95), timeout=5)

    assert asyncio.run(scenario()) is None
    assert "no llegó" in bot._entry_abort_reason
    assert len(consultas) == 1  # no esperó los 30 s del siguiente ciclo


def test_snapshot_expone_el_plazo_de_la_baja():
    import time
    client = _FeedQueSeCalla(events_by_connection=[[{"price": 1.0}, {"price": 0.94}]])
    cfg = make_config(entry_dip_pct=5.0, entry_dip_timeout_minutes=360.0)
    bot = TrailingTakeProfitBot(client, SpyExecutor(), cfg)

    async def scenario():
        await bot._stream.connect()
        await bot._get_initial_price()

    antes = time.time()
    asyncio.run(scenario())
    snap = bot.snapshot()
    assert antes + 360 * 60 - 1 <= snap["entry_dip_deadline"] <= time.time() + 360 * 60
    assert snap["settings"]["entry_dip_timeout_minutes"] == 360.0


def test_fmt_minutes():
    from pepump.bot import _fmt_minutes
    assert _fmt_minutes(360) == "6 h"
    assert _fmt_minutes(90) == "1 h 30 min"
    assert _fmt_minutes(20) == "20 min"
    assert _fmt_minutes(0.5) == "0.5 min"


# ---- Entrar ya (enter_now) ------------------------------------------------ #
# Mientras se espera la baja, el botón "Entrar ya" de la web compra al precio
# actual sin esperar más.


def _bot_esperando_por_feed(events):
    client = _FeedQueSeCalla(events_by_connection=[events])
    cfg = make_config(entry_dip_pct=5.0, entry_dip_timeout_minutes=0.0)
    bot = TrailingTakeProfitBot(client, SpyExecutor(), cfg)
    bot.running = True
    return bot, client


async def _esperar_baja(bot):
    for _ in range(200):
        if bot.can_enter_now:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("el bot no llegó a esperar la baja")


def test_enter_now_compra_al_ultimo_precio_del_feed_sin_romper_el_generador():
    bot, client = _bot_esperando_por_feed([{"price": 1.0}, {"price": 0.97}])

    async def scenario():
        await bot._stream.connect()
        bot.phase = "waiting_entry"
        tarea = asyncio.create_task(bot._get_initial_price())
        await _esperar_baja(bot)
        await asyncio.sleep(0.02)  # que llegue el 0.97
        bot.enter_now("prueba")
        return await asyncio.wait_for(tarea, timeout=5)

    assert asyncio.run(scenario()) == 0.97
    assert client.connect_calls == 1
    assert not bot.can_enter_now
    assert bot.snapshot()["enter_now_requested"] is True


def test_enter_now_compra_de_verdad_por_enter():
    bot, _ = _bot_esperando_por_feed([{"price": 1.0}])
    executor = bot.executor

    async def scenario():
        await bot._stream.connect()
        tarea = asyncio.create_task(bot._enter())
        await _esperar_baja(bot)
        bot.enter_now()
        return await asyncio.wait_for(tarea, timeout=5)

    assert asyncio.run(scenario()) is True
    assert executor.buy_calls == 1
    assert bot.phase == "open"


def test_enter_now_con_precio_on_chain_no_espera_al_siguiente_ciclo():
    """Rama on-chain: el polling cada 30 s se corta y se consulta un precio
    fresco para comprar."""
    cfg = make_config(entry_dip_pct=5.0, entry_dip_timeout_minutes=0.0, onchain_poll_interval_seconds=30.0)
    bot = TrailingTakeProfitBot(None, SpyExecutor(), cfg)
    bot.running = True
    bot.phase = "waiting_entry"
    bot.onchain.source = "pumpswap"
    precios = iter([0.99, 0.98])

    async def fetch_price():
        return next(precios)

    bot.onchain.fetch_price = fetch_price

    async def scenario():
        bot._dip_target = 0.95
        tarea = asyncio.create_task(bot._wait_for_dip_entry(1.0, 0.95))
        await asyncio.sleep(0.05)
        bot.enter_now()
        return await asyncio.wait_for(tarea, timeout=2)

    assert asyncio.run(scenario()) == 0.98


def test_enter_now_solo_mientras_se_espera_la_baja():
    import pytest
    cfg = make_config(entry_dip_pct=5.0)
    bot = TrailingTakeProfitBot(FakeTradeStreamClient(), SpyExecutor(), cfg)
    bot.running = True
    bot.phase = "waiting_entry"
    # Sin objetivo todavía (buscando la referencia): no se puede.
    with pytest.raises(ValueError):
        bot.enter_now()
    bot._dip_target = 0.95
    assert bot.can_enter_now
    bot.close("cancelado")
    assert not bot.can_enter_now
    with pytest.raises(ValueError):
        bot.enter_now()
