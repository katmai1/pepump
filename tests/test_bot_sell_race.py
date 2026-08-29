"""
Regresión sobre el bug MÁS IMPORTANTE encontrado: al pedir shutdown
(Ctrl+C/SIGTERM) con una posición abierta, _sell_on_shutdown() podía
correr en simultáneo con el monitor de precios (_consume_trade_stream /
_poll_onchain_price_loop), que seguía vivo y podía disparar su propio
_try_sell() si un precio cruzaba el trailing-stop en esa misma ventana.
Resultado: dos llamadas a executor.sell() para la MISMA posición podían
salir en paralelo hacia la Lightning API con SOL real.

El fix tiene dos partes, y estos tests cubren ambas:
  1. run() ahora cancela monitor_task/status_printer ANTES de llamar a
     _sell_on_shutdown (ver test_bot_run_flow.py).
  2. _sell_lock serializa CUALQUIER intento de venta (_try_sell y
     _sell_on_shutdown), como red de seguridad adicional para el caso
     límite en que una venta ya esté "en vuelo" (el POST real ya salió
     vía asyncio.to_thread) cuando se pide cancelar esa tarea.
"""
import asyncio

from pepump.bot import TrailingTakeProfitBot

from .conftest import FakeTradeStreamClient, SpyExecutor, make_config


async def test_concurrent_monitor_sell_and_shutdown_sell_only_hit_executor_once(open_position):
    """
    El test clave: dispara _try_sell (como haría el monitor al ver el
    precio cruzar el trailing-stop) y _sell_on_shutdown (como haría el
    cierre por Ctrl+C) EN SIMULTÁNEO sobre la misma posición. Sin el
    lock, ambas corrutinas pasarían el chequeo `if pos.closed` (todavía
    en False para las dos) antes de que la primera termine, y
    executor.sell() se llamaría dos veces.
    """
    cfg = make_config()
    client = FakeTradeStreamClient()
    # sell_delay simula la latency real de la Lightning API + confirmación
    # on-chain: le da tiempo a la segunda corrutina a "alcanzar" a la
    # primera si el lock no estuviera.
    executor = SpyExecutor(sell_delay=0.05)
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot.position = open_position
    bot.latest_price = 1.5

    await asyncio.gather(
        bot._try_sell(bot.position, 1.5, "retroceso de trailing-stop (monitor)"),
        bot._sell_on_shutdown(),
    )

    assert executor.sell_calls == 1, (
        "executor.sell() se llamó más de una vez para la misma posición: "
        "la venta NO está serializada correctamente."
    )
    assert bot.position.closed is True


async def test_sell_on_shutdown_is_noop_if_position_already_closed(open_position):
    cfg = make_config()
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot.position = open_position
    bot.latest_price = 1.5
    bot.position.closed = True  # ya se cerró por otro camino

    await bot._sell_on_shutdown()

    assert executor.sell_calls == 0


async def test_sell_on_shutdown_is_noop_if_no_position():
    cfg = make_config()
    client = FakeTradeStreamClient()
    executor = SpyExecutor()
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot.position = None

    await bot._sell_on_shutdown()  # no debe explotar

    assert executor.sell_calls == 0


async def test_try_sell_failure_keeps_position_open_and_does_not_set_closed_event(open_position):
    cfg = make_config()
    client = FakeTradeStreamClient()
    executor = SpyExecutor(fail_sell_times=1)  # la primera venta falla
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)
    bot.position = open_position

    await bot._try_sell(bot.position, 1.5, "trailing-stop")

    assert bot.position.closed is False
    assert bot._closed_event.is_set() is False
    assert executor.sell_calls == 1


async def test_wait_for_close_or_shutdown_returns_false_when_position_closes_on_its_own():
    cfg = make_config()
    bot = TrailingTakeProfitBot(
        client=FakeTradeStreamClient(), executor=SpyExecutor(), config=cfg
    )

    async def close_soon():
        await asyncio.sleep(0.01)
        bot._closed_event.set()

    asyncio.create_task(close_soon())
    result = await bot._wait_for_close_or_shutdown()

    assert result is False  # se cerró sola (TP/SL): run() no debe vender de nuevo


async def test_wait_for_close_or_shutdown_returns_true_when_shutdown_requested():
    cfg = make_config()
    bot = TrailingTakeProfitBot(
        client=FakeTradeStreamClient(), executor=SpyExecutor(), config=cfg
    )

    async def shutdown_soon():
        await asyncio.sleep(0.01)
        bot._shutdown_requested.set()

    asyncio.create_task(shutdown_soon())
    result = await bot._wait_for_close_or_shutdown()

    assert result is True  # run() debe disparar la venta de cierre
