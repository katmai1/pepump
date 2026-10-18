"""
Tests del control del bot "desde fuera" (paso previo al panel web):
close(), snapshot() y la etiqueta de mint en los logs.
"""
import asyncio
import json
import logging

from pepump.bot import TrailingTakeProfitBot
from pepump.executor import Position, TradeExecutor
from pepump.log_context import MintContextFilter

from .conftest import FakeTradeStreamClient, SpyExecutor, make_config


def make_bot(events=None, **cfg_overrides):
    cfg = make_config(status_interval_seconds=999, **cfg_overrides)
    client = FakeTradeStreamClient(events_by_connection=[events or [{"price": 1.0}]])
    executor = SpyExecutor()
    return TrailingTakeProfitBot(client=client, executor=executor, config=cfg), executor


async def _esperar_posicion(bot):
    while bot.position is None:
        await asyncio.sleep(0.005)


async def test_close_vende_con_el_motivo_pedido():
    bot, executor = make_bot()

    async def cerrar():
        await _esperar_posicion(bot)
        bot.close("cerrado desde la web")

    asyncio.create_task(cerrar())
    await asyncio.wait_for(bot.run(), timeout=5)

    assert executor.sell_calls == 1
    assert executor.last_sell_reason == "cierre manual (cerrado desde la web)"


class _FeedEnSilencio(FakeTradeStreamClient):
    """Conexión abierta que no entrega nada (el FakeTradeStreamClient base
    cierra el stream en cuanto agota sus eventos)."""

    async def iter_trade_events(self, ws):
        await asyncio.Event().wait()
        yield  # pragma: no cover


async def test_close_antes_de_comprar_no_compra():
    # Sin eventos: el bot se queda esperando el primer precio.
    cfg = make_config(status_interval_seconds=999)
    client = _FeedEnSilencio()
    executor = SpyExecutor()
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)

    task = asyncio.create_task(bot.run())
    while bot.phase != "waiting_entry":
        await asyncio.sleep(0.005)
    bot.close("cancelado")
    await asyncio.wait_for(task, timeout=5)

    assert executor.buy_calls == 0
    snap = bot.snapshot()
    assert snap["phase"] == "stopped"
    assert snap["running"] is False
    assert "cancelado" in snap["end_reason"]


async def test_segundo_close_no_cambia_el_motivo():
    bot, executor = make_bot()

    async def cerrar_dos_veces():
        await _esperar_posicion(bot)
        bot.close("primero")
        bot.close("segundo")

    asyncio.create_task(cerrar_dos_veces())
    await asyncio.wait_for(bot.run(), timeout=5)

    assert executor.sell_calls == 1
    assert executor.last_sell_reason == "cierre manual (primero)"


async def test_snapshot_con_posicion_abierta_y_armada():
    # 1.0 compra; 1.3 arma el trailing (activación default < 30%).
    bot, _ = make_bot(events=[{"price": 1.0}, {"price": 1.3}], activation_pct=20.0,
                      trailing_pct=10.0, initial_stop_pct=25.0, strategy_price_basis="market")
    task = asyncio.create_task(bot.run())
    await _esperar_posicion(bot)
    while not bot.position.armed:
        await asyncio.sleep(0.005)

    snap = bot.snapshot()
    json.dumps(snap)  # tiene que poder ir tal cual a la web
    assert snap["phase"] == "open"
    assert snap["running"] is True
    assert snap["mint"] == bot.mint
    pos = snap["position"]
    assert pos["armed"] is True
    assert pos["highest_price"] == 1.3
    assert abs(pos["activation_price"] - 1.2) < 1e-9
    assert abs(pos["initial_stop_price"] - 0.75) < 1e-9
    assert abs(pos["trailing_stop_price"] - 1.17) < 1e-9
    assert abs(pos["market_pnl_pct"] - 30.0) < 1e-6

    bot.close("fin del test")
    await asyncio.wait_for(task, timeout=5)
    assert bot.snapshot()["phase"] == "closed"


async def test_snapshot_antes_de_arrancar():
    bot, _ = make_bot()
    snap = bot.snapshot()
    json.dumps(snap)
    assert snap["phase"] == "starting"
    assert snap["position"] is None
    assert snap["running"] is False


async def test_sell_guarda_los_datos_del_cierre_en_la_posicion(tmp_path):
    cfg = make_config(live=False, trade_history_csv="", sim_costs=False)
    executor = TradeExecutor(client=None, live=False, config=cfg)
    pos = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    await executor.sell(pos, 1.5, "test")

    assert pos.closed is True
    assert pos.exit_price == 1.5
    assert abs(pos.realized_pnl_pct - 50.0) < 1e-9
    assert abs(pos.realized_pnl_sol - 0.025) < 1e-9
    assert pos.close_reason == "test"
    assert pos.closed_at is not None


class _Captura(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.addFilter(MintContextFilter())

    def emit(self, record):
        self.records.append(record)


async def test_los_logs_del_bot_llevan_su_mint():
    handler = _Captura()
    root = logging.getLogger()
    root.addHandler(handler)
    nivel_previo = root.level
    root.setLevel(logging.INFO)
    try:
        logging.getLogger("pepump.test").info("antes de arrancar")
        bot, _ = make_bot()

        async def cerrar():
            await _esperar_posicion(bot)
            bot.close("fin")

        asyncio.create_task(cerrar())
        await asyncio.wait_for(bot.run(), timeout=5)
        logging.getLogger("pepump.test").info("después de terminar")
    finally:
        root.removeHandler(handler)
        root.setLevel(nivel_previo)

    fuera = [r for r in handler.records if r.name == "pepump.test"]
    assert [r.mint for r in fuera] == ["", ""]
    del_bot = [r for r in handler.records if r.name == "pepump.bot"]
    assert del_bot and all(r.mint == bot.mint for r in del_bot)
