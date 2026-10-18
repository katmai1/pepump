"""Reintentos de una venta fallida: cada fallo sube el slippage y el
priority fee del siguiente intento (hasta los topes), con una pausa corta
entre intentos y un aviso al acumular varios fallos seguidos."""
import asyncio
import logging

import pytest

from pepump.bot import TrailingTakeProfitBot
from pepump.config import validate_config
from pepump.executor import Position, TradeExecutor, sell_retry_params
from tests.conftest import FakeTradeStreamClient, SpyExecutor, make_config


def _pos():
    return Position(mint="M", entry_price=1.0, sol_amount=0.05, token_amount=0.05)


# --- sell_retry_params ------------------------------------------------- #

def test_sin_fallos_usa_la_config_tal_cual():
    cfg = make_config(slippage=15.0, priority_fee=0.00001)
    assert sell_retry_params(cfg, 0) == (15.0, 0.00001)


def test_cada_fallo_sube_slippage_y_fee_hasta_los_topes():
    cfg = make_config(slippage=15.0, priority_fee=0.00001,
                      max_sell_slippage=50.0, max_sell_priority_fee=0.001)
    slippages = [sell_retry_params(cfg, n)[0] for n in range(6)]
    fees = [sell_retry_params(cfg, n)[1] for n in range(6)]
    assert slippages == [15.0, 25.0, 35.0, 45.0, 50.0, 50.0]
    assert fees == pytest.approx([0.00001, 0.00004, 0.00016, 0.00064, 0.001, 0.001])


def test_el_tope_nunca_baja_el_valor_de_la_posicion():
    # Posición abierta desde la web con más slippage que el tope.
    cfg = make_config(slippage=60.0, priority_fee=0.005,
                      max_sell_slippage=50.0, max_sell_priority_fee=0.001)
    assert sell_retry_params(cfg, 0) == (60.0, 0.005)
    assert sell_retry_params(cfg, 3) == (60.0, 0.005)


@pytest.mark.parametrize("valor", [0, -5, 101])
def test_max_sell_slippage_fuera_de_rango_es_error(valor):
    with pytest.raises(ValueError, match="max_sell_slippage"):
        validate_config(make_config(max_sell_slippage=valor))


# --- TradeExecutor.sell -------------------------------------------------- #

class _LightningSpy:
    def __init__(self):
        self.calls = []

    async def execute_lightning_trade(self, **kwargs):
        self.calls.append(kwargs)
        return {"signature": "sig"}


async def test_executor_manda_el_slippage_y_fee_del_reintento():
    client = _LightningSpy()
    executor = TradeExecutor(client=client, live=True, config=make_config(trade_history_csv=""))
    await executor.sell(_pos(), 1.0, "stop", slippage=35.0, priority_fee=0.00016)
    assert client.calls[0]["slippage"] == 35.0
    assert client.calls[0]["priority_fee"] == 0.00016


async def test_executor_sin_reintento_usa_la_config():
    client = _LightningSpy()
    executor = TradeExecutor(client=client, live=True,
                             config=make_config(slippage=15.0, priority_fee=0.00001, trade_history_csv=""))
    await executor.sell(_pos(), 1.0, "stop")
    assert (client.calls[0]["slippage"], client.calls[0]["priority_fee"]) == (15.0, 0.00001)


# --- bot ------------------------------------------------------------------ #

def _bot(fail_sell_times):
    executor = SpyExecutor(fail_sell_times=fail_sell_times)
    cfg = make_config(slippage=15.0, priority_fee=0.00001,
                      max_sell_slippage=50.0, max_sell_priority_fee=0.001)
    bot = TrailingTakeProfitBot(FakeTradeStreamClient(), executor, cfg)
    bot.position = _pos()
    return bot, executor


async def test_reintenta_con_mas_slippage_hasta_vender():
    bot, executor = _bot(fail_sell_times=2)
    for _ in range(3):
        await bot._try_sell(bot.position, 0.7, "stop-loss")

    assert bot.position.closed
    assert executor.sell_params == pytest.approx([(15.0, 0.00001), (25.0, 0.00004), (35.0, 0.00016)])
    assert bot.sell_failures == 0  # se reinicia al vender
    assert bot.snapshot()["sell_failures"] == 0


async def test_respeta_la_pausa_entre_reintentos(monkeypatch):
    monkeypatch.setattr(TrailingTakeProfitBot, "_SELL_RETRY_DELAY_SECONDS", 60.0)
    bot, executor = _bot(fail_sell_times=1)
    await bot._try_sell(bot.position, 0.7, "stop-loss")
    await bot._try_sell(bot.position, 0.69, "stop-loss")  # todavía en la pausa

    assert executor.sell_calls == 1
    assert not bot.position.closed
    assert bot.snapshot()["sell_failures"] == 1


async def test_avisa_en_error_tras_varios_fallos_seguidos(caplog):
    bot, _ = _bot(fail_sell_times=99)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await bot._try_sell(bot.position, 0.7, "stop-loss")

    alertas = [r for r in caplog.records if r.levelno == logging.ERROR and "ventas fallidas seguidas" in r.message]
    assert len(alertas) == 1
    assert not bot.position.closed


async def test_la_venta_de_cierre_tambien_sube_el_slippage():
    bot, executor = _bot(fail_sell_times=2)
    bot.latest_price = 1.0
    await asyncio.wait_for(bot._sell_on_shutdown(), 5)

    assert bot.position.closed
    assert [s for s, _ in executor.sell_params] == [15.0, 25.0, 35.0]
