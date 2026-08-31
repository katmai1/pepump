"""
Test para el BUGFIX de la orden que revierte on-chain con el error 6005
(BondingCurveComplete) del programa Pump cuando el bot ya confirmó -por
el fallback on-chain- que el mint migró a PumpSwap.

Antes de este fix, TradeExecutor.buy()/sell() siempre mandaban
pool=self.cfg.pool ("auto" por default) a la Lightning API, incluso
cuando bot.py ya sabía con certeza (self._using_onchain_fallback) que
la bonding curve de ese mint ya no existe. PumpPortal, con pool="auto",
podía seguir intentando rutear por la bonding curve vieja y la orden
revertía on-chain: la transacción confirmaba (200 OK de la Lightning
API) pero fallaba con el error 6005, y la compra terminaba sin abrir
posición (o la venta sin cerrarla) pese a que el precio de referencia
sí venía del pool de PumpSwap real.

Ahora bot.py debe pasar pool_override="pump-amm" en cuanto
_using_onchain_fallback sea True -tanto para la compra que dispara el
fallback de entrada como para cualquier venta posterior-, y NO pasar
override (pool_override=None, se usa self.cfg.pool tal cual) mientras
el feed en vivo sigue funcionando normalmente.
"""
import asyncio

from pepump.bot import TrailingTakeProfitBot
from pepump.executor import Position
from tests.conftest import SpyExecutor, make_config


def test_compra_via_fallback_de_entrada_fuerza_pool_pump_amm():
    """Si _get_initial_price() tuvo que recurrir al fallback on-chain
    (el mint ya migró antes de que el bot empezara a seguirlo), la
    compra debe pedir explícitamente pool="pump-amm", no confiar en
    cfg.pool="auto"."""
    executor = SpyExecutor()
    cfg = make_config(pool="auto")
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)

    # Simula lo que hace _try_onchain_fallback cuando SÍ encuentra un
    # pool de PumpSwap con precio real: marca el flag antes de comprar.
    bot._using_onchain_fallback = True

    asyncio.run(bot._on_first_price(0.0000009138))

    assert executor.buy_calls == 1
    assert executor.last_buy_pool_override == "pump-amm"


def test_compra_por_feed_en_vivo_no_fuerza_pool():
    """Si el precio de entrada vino del feed en vivo normal (sin pasar
    por el fallback), no hay que tocar cfg.pool: se manda
    pool_override=None y execute_lightning_trade usa cfg.pool tal cual
    (sigue siendo "auto" salvo que el usuario lo haya configurado
    distinto a propósito)."""
    executor = SpyExecutor()
    cfg = make_config(pool="auto")
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)

    assert bot._using_onchain_fallback is False  # default

    asyncio.run(bot._on_first_price(0.0000009138))

    assert executor.buy_calls == 1
    assert executor.last_buy_pool_override is None


def test_venta_con_fallback_activo_fuerza_pool_pump_amm():
    """Una vez que _using_onchain_fallback quedó en True (sea por el
    fallback de entrada o por una migración detectada a mitad de
    posición), CUALQUIER venta posterior -incluida la de cierre por
    trailing-stop/stop-loss- también debe forzar pool="pump-amm"."""
    executor = SpyExecutor()
    cfg = make_config(pool="auto", activation_pct=10.0, trailing_pct=15.0, initial_stop_pct=25.0)
    bot = TrailingTakeProfitBot(client=None, executor=executor, config=cfg)
    bot._using_onchain_fallback = True
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    # Precio por debajo del stop-loss inicial -> dispara _try_sell.
    asyncio.run(bot._on_price_update(0.70))

    assert executor.sell_calls == 1
    assert executor.last_sell_pool_override == "pump-amm"
    assert bot.position.closed is True
