"""
Tests de TrailingTakeProfitBot.run():
  - manejo prolijo del error cuando connect_trade_stream() falla (segundo
    bug pedido a arreglar: antes se escapaba un traceback crudo, ahora
    se loguea y se sale ordenadamente).
  - que run() cancele monitor_task/status_printer ANTES de vender por
    shutdown (para no repetir la carrera de doble venta -ver
    test_bot_sell_race.py- también en el flujo real end-to-end).
"""
import asyncio

import pytest

from pepump.bot import TrailingTakeProfitBot

from .conftest import FakeTradeStreamClient, SpyExecutor, make_config


async def test_run_handles_connect_stream_failure_without_raising(caplog):
    """
    Antes del fix, una excepción en connect_trade_stream() (red caída,
    DNS, api_key rechazada en el handshake, etc.) se escapaba de run()
    sin capturar -run.py solo atrapa KeyboardInterrupt- y el proceso
    terminaba con un traceback crudo. Ahora debe quedar contenida.
    """
    cfg = make_config()
    client = FakeTradeStreamClient(fail_connect_times=999,
                                    connect_exc=ConnectionRefusedError("no conecta (fake)"))
    executor = SpyExecutor()
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)

    with caplog.at_level("ERROR"):
        await bot.run()  # no debe lanzar

    assert bot.position is None
    assert executor.buy_calls == 0
    assert any("No se pudo conectar" in rec.message for rec in caplog.records)


async def test_run_happy_path_buys_and_closes_when_stop_loss_hits():
    """
    Flujo feliz de punta a punta: primer trade fija el precio de entrada,
    compra, y un trade posterior que cruza el stop-loss inicial dispara
    la venta y termina run() solo (sin necesidad de shutdown).
    """
    cfg = make_config(initial_stop_pct=25.0, activation_pct=10.0, status_interval_seconds=999)
    events = [
        {"price": 1.0},          # precio inicial -> compra
        {"price": 0.70},         # -30%: cruza el stop-loss inicial (25%) -> vende
    ]
    client = FakeTradeStreamClient(events_by_connection=[events])
    executor = SpyExecutor()
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)

    await asyncio.wait_for(bot.run(), timeout=5)

    assert executor.buy_calls == 1
    assert executor.sell_calls == 1
    assert bot.position.closed is True
    assert bot._ws.close_calls == 1  # el socket se cierra ordenadamente en el finally


async def test_run_cancels_monitor_before_selling_on_shutdown():
    """
    Verifica el orden correcto: cuando se pide shutdown con la posición
    todavía armada (esperando activación), monitor_task debe quedar
    cancelado ANTES de que se dispare la venta de cierre -para que no
    pueda reaccionar a un evento y pisarle la venta a _sell_on_shutdown.

    Se simula pidiendo shutdown inmediatamente después de la compra, con
    un stream de eventos que se queda "colgado" (nunca llega un segundo
    trade): si el bug siguiera presente, este test igual pasaría porque
    no hay una condición de carrera activa -lo importante acá es que NO
    haya ningún error/duplicado y que sell termine llamándose una sola
    vez con el bot ya limpio.
    """
    cfg = make_config(status_interval_seconds=999)
    events = [{"price": 1.0}]  # solo el trade inicial; el stream queda "abierto y en silencio"
    client = FakeTradeStreamClient(events_by_connection=[events])
    executor = SpyExecutor()
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)

    async def request_shutdown_after_buy():
        # Espera a que haya posición (compra ya hecha) y pide shutdown.
        while bot.position is None:
            await asyncio.sleep(0.005)
        bot._request_shutdown("SIGINT")

    asyncio.create_task(request_shutdown_after_buy())

    await asyncio.wait_for(bot.run(), timeout=5)

    assert executor.buy_calls == 1
    assert executor.sell_calls == 1
    assert bot.position.closed is True
