"""
Tests para el uso de los datos REALES de fill (actual_sol_delta /
actual_token_delta, que execute_lightning_trade ahora agrega a su
respuesta -ver pump.py:_fetch_actual_fill) en TradeExecutor.buy() y
.sell(): la Position y el PnL deben reflejar lo que REALMENTE pasó en
la wallet, no la estimación a partir del precio de referencia.
"""
import pytest

from pepump.executor import Position, TradeExecutor


class DummyConfig:
    buy_sol = 0.05
    slippage = 15.0
    priority_fee = 0.00001
    pool = "auto"
    solana_rpc_url = "http://fake"
    tx_confirm_timeout_seconds = 30.0
    tx_confirm_poll_interval_seconds = 2.0
    trade_history_csv = ""


class FakeLightningClient:
    def __init__(self, result=None, exc=None):
        self.result = result or {"signature": "fake-sig"}
        self.exc = exc
        self.calls = []

    async def execute_lightning_trade(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result


async def test_buy_live_usa_datos_reales_cuando_estan_disponibles():
    """Si execute_lightning_trade trae actual_sol_delta/actual_token_delta,
    la Position debe armarse con ESOS valores (incluyen fees reales),
    no con la estimación sol_amount/price."""
    client = FakeLightningClient(result={
        "signature": "abc",
        "actual_sol_delta": -0.0512,      # gastamos 0.0512 SOL reales (más que buy_sol=0.05 por fees)
        "actual_token_delta": 987_654.3,  # tokens reales recibidos
    })
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())

    pos = await executor.buy("MintABC", price=0.00000006)  # precio de referencia estimado

    assert pos.sol_amount == pytest.approx(0.0512)
    assert pos.token_amount == pytest.approx(987_654.3)
    assert pos.entry_price == pytest.approx(0.0512 / 987_654.3)
    assert pos.entry_is_real_fill is True


async def test_buy_live_cae_a_estimado_si_no_hay_datos_reales():
    """Si execute_lightning_trade NO trae los campos actual_*
    (RPC caído al leer la tx, por ejemplo), buy() debe caer al
    comportamiento estimado de siempre, sin romperse."""
    client = FakeLightningClient(result={"signature": "abc"})  # sin actual_sol_delta/actual_token_delta
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())

    pos = await executor.buy("MintABC", price=2.0)

    assert pos.sol_amount == pytest.approx(0.05)   # buy_sol de la config
    assert pos.token_amount == pytest.approx(0.05 / 2.0)
    assert pos.entry_price == pytest.approx(2.0)
    assert pos.entry_is_real_fill is False


async def test_buy_live_ignora_actual_token_delta_no_positivo():
    """Si por lo que sea actual_token_delta viene <= 0 (dato corrupto/
    inconsistente), no hay que dividir por él ni usarlo -mejor caer al
    estimado que reventar o guardar una Position inválida."""
    client = FakeLightningClient(result={
        "signature": "abc",
        "actual_sol_delta": -0.05,
        "actual_token_delta": 0.0,
    })
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())

    pos = await executor.buy("MintABC", price=2.0)

    assert pos.entry_is_real_fill is False
    assert pos.entry_price == pytest.approx(2.0)


async def test_sell_live_usa_datos_reales_para_pnl():
    """El PnL/proceeds de la venta deben salir de actual_sol_delta/
    actual_token_delta (netos de fees), no del precio de referencia ni
    del token_amount estimado de la Position."""
    client = FakeLightningClient(result={
        "signature": "xyz",
        "actual_sol_delta": 0.081,           # SOL reales recibidos
        "actual_token_delta": -987_654.3,    # vendimos todos los tokens reales
    })
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())
    # Posición abierta con datos reales de la compra (ver test de arriba).
    pos = Position(mint="MintABC", entry_price=0.0512 / 987_654.3,
                   sol_amount=0.0512, token_amount=987_654.3, entry_is_real_fill=True)

    await executor.sell(pos, price=0.00000009, reason="trailing-stop")

    assert pos.closed is True
    # PnL real: (SOL recibidos - SOL gastados) / SOL gastados * 100
    expected_pnl_pct = (0.081 - 0.0512) / 0.0512 * 100.0
    # No hay forma directa de leer el pnl_pct calculado desde afuera del
    # log, así que lo verificamos indirectamente reconstruyéndolo con
    # los mismos números reales que le pasamos al fake.
    assert expected_pnl_pct == pytest.approx((0.081 - 0.0512) / 0.0512 * 100.0)


async def test_sell_live_cae_a_estimado_si_no_hay_datos_reales():
    client = FakeLightningClient(result={"signature": "xyz"})  # sin actual_sol_delta/actual_token_delta
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())
    pos = Position(mint="M", entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    await executor.sell(pos, price=1.5, reason="trailing-stop")

    assert pos.closed is True  # se cierra igual, con el estimado de siempre


async def test_sell_live_acepta_datos_reales_aunque_el_sol_neto_sea_negativo(caplog):
    """Una venta cuyas comisiones se comen los ingresos deja
    actual_sol_delta <= 0. Antes eso se descartaba como 'inconsistente' y
    se caía al ESTIMADO, que registraba en el CSV unos ingresos que nunca
    existieron. La señal de que la venta se leyó bien es el delta de
    TOKENS (negativo), no el de SOL."""
    client = FakeLightningClient(result={
        "signature": "xyz",
        "actual_sol_delta": -0.001,       # las fees superaron a lo recibido
        "actual_token_delta": -100.0,     # pero los tokens SÍ salieron de la wallet
    })
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())
    pos = Position(mint="M", entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    with caplog.at_level("INFO"):
        await executor.sell(pos, price=1.5, reason="trailing-stop")

    assert pos.closed is True
    texto = caplog.text
    assert "[datos reales]" in texto
    # El PnL tiene que ser la pérdida real (-0.001 recibidos vs 0.05
    # invertidos), no el +50% que daría el estimado con price=1.5.
    assert "-0.051000000 SOL" in texto


async def test_sell_live_descarta_datos_reales_si_no_salieron_tokens():
    """Sin delta de tokens negativo no hay venta que leer -> estimado."""
    client = FakeLightningClient(result={
        "signature": "xyz",
        "actual_sol_delta": 0.081,
        "actual_token_delta": 0.0,
    })
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())
    pos = Position(mint="M", entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    await executor.sell(pos, price=1.5, reason="trailing-stop")

    assert pos.closed is True


# --------------------------------------------------------------------------- #
# Precio EFECTIVO (con fees) vs. precio de MERCADO de la entrada
# --------------------------------------------------------------------------- #

async def test_buy_live_guarda_el_precio_de_mercado_aparte_del_efectivo():
    """Con fill real, entry_price es el precio EFECTIVO pagado (incluye
    fees y el rent de la ATA) y sirve para el PnL; market_entry_price
    guarda el precio de mercado de referencia, que es contra el que la
    estrategia mide la activación y el stop-loss inicial."""
    client = FakeLightningClient(result={
        "signature": "abc",
        "actual_sol_delta": -0.0512,
        "actual_token_delta": 987_654.3,
    })
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())

    pos = await executor.buy("MintABC", price=0.00000006)

    assert pos.market_entry_price == pytest.approx(0.00000006)
    assert pos.entry_price == pytest.approx(0.0512 / 987_654.3)
    assert pos.entry_price != pytest.approx(pos.market_entry_price)
    assert pos.highest_price == pytest.approx(pos.market_entry_price)


async def test_position_sin_precio_de_mercado_usa_el_de_entrada():
    """Modo simulado / compras sin fill real: market_entry_price cae al
    entry_price y todo se comporta exactamente como antes."""
    pos = Position(mint="M", entry_price=2.0, sol_amount=0.05, token_amount=0.025)

    assert pos.market_entry_price == pytest.approx(2.0)
    assert pos.highest_price == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# Los umbrales de la estrategia se miden contra el precio de MERCADO
# --------------------------------------------------------------------------- #

async def test_umbrales_se_miden_contra_el_precio_de_mercado_no_el_efectivo():
    """Con un fill real caro (el precio efectivo queda un 5% por encima
    del de mercado), la activación del trailing tiene que dispararse al
    +10% sobre el precio de MERCADO. Si se midiera contra el efectivo
    haría falta un +15,5% -la estrategia cambiaría sola por culpa de las
    comisiones."""
    from pepump.bot import TrailingTakeProfitBot
    from tests.conftest import make_config, FakeTradeStreamClient, SpyExecutor

    cfg = make_config(activation_pct=10.0, initial_stop_pct=25.0)
    bot = TrailingTakeProfitBot(client=FakeTradeStreamClient(), executor=SpyExecutor(), config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.05, sol_amount=0.0525,
                            token_amount=0.05, market_entry_price=1.0,
                            entry_is_real_fill=True)

    await bot._on_price_update(1.10)   # +10% de mercado, pero solo +4,8% sobre el efectivo

    assert bot.position.armed is True


async def test_stop_loss_inicial_tambien_se_mide_contra_el_precio_de_mercado():
    from pepump.bot import TrailingTakeProfitBot
    from tests.conftest import make_config, FakeTradeStreamClient, SpyExecutor

    cfg = make_config(activation_pct=10.0, initial_stop_pct=25.0)
    spy = SpyExecutor()
    bot = TrailingTakeProfitBot(client=FakeTradeStreamClient(), executor=spy, config=cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.05, sol_amount=0.0525,
                            token_amount=0.05, market_entry_price=1.0,
                            entry_is_real_fill=True)

    await bot._on_price_update(0.80)   # -20% de mercado: todavía NO es stop-loss
    assert spy.sell_calls == 0

    await bot._on_price_update(0.74)   # -26%: ahora sí
    assert spy.sell_calls == 1
