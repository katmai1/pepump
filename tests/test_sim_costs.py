"""Modo simulado con los costes de una operación real (general.sim_costs):
comisiones, slippage en contra, priority fee y fee de red, para que el PnL
simulado no salga más optimista que el real. El rent de la cuenta del token
no: no es un coste (ver executor.Position)."""
import pytest

from pepump.config import validate_config
from pepump.executor import NETWORK_FEE_SOL, TradeExecutor, simulated_buy, simulated_sell
from tests.conftest import make_config


def _cfg(**kw):
    base = dict(buy_sol=0.05, priority_fee=0.00001, sim_fee_pct=2.0, sim_slippage_pct=1.0,
                trade_history_csv="")
    base.update(kw)
    return make_config(**base)


def test_compra_simulada_paga_slippage_comisiones_y_costes_fijos():
    cfg = _cfg()
    spent, tokens, entry = simulated_buy(cfg, 1.0)
    assert tokens == pytest.approx(0.05 / 1.01)
    assert spent == pytest.approx(0.05 * 1.02 + 0.00001 + NETWORK_FEE_SOL)
    assert entry == pytest.approx(spent / tokens)
    assert entry > 1.0


def test_el_rent_no_infla_el_precio_de_entrada():
    """Con 0.05 SOL, el rent (~0.002 SOL) es un 4%: contarlo como coste
    retrasaba la activación del trailing y adelantaba el stop-loss. No es un
    coste: es un depósito que el bot recupera cerrando la cuenta."""
    cfg = _cfg(sim_fee_pct=0.0, sim_slippage_pct=0.0, priority_fee=0.0)
    spent, tokens, entry = simulated_buy(cfg, 1.0)
    assert spent == pytest.approx(0.05 + NETWORK_FEE_SOL)
    assert entry == pytest.approx(1.0 + NETWORK_FEE_SOL / 0.05)


def test_venta_simulada_cobra_slippage_comisiones_y_fees():
    cfg = _cfg()
    proceeds, exit_price = simulated_sell(cfg, 0.05, 1.0)
    esperado = 0.05 * 0.99 * 0.98 - 0.00001 - NETWORK_FEE_SOL
    assert proceeds == pytest.approx(esperado)
    assert exit_price == pytest.approx(esperado / 0.05)


def test_venta_simulada_usa_el_priority_fee_del_reintento():
    cfg = _cfg()
    base, _ = simulated_sell(cfg, 0.05, 1.0)
    caro, _ = simulated_sell(cfg, 0.05, 1.0, priority_fee=0.001)
    assert base - caro == pytest.approx(0.001 - 0.00001)


def test_sin_costes_ni_ingresos_el_precio_de_salida_es_el_de_mercado():
    cfg = _cfg(priority_fee=1.0)  # el fee se come todo
    proceeds, exit_price = simulated_sell(cfg, 0.05, 1e-6)
    assert proceeds < 0
    assert exit_price == 1e-6


async def test_ida_y_vuelta_al_mismo_precio_pierde_los_costes():
    """Comprar y vender sin que el precio se mueva: con costes se pierde
    (como en real); sin costes, PnL 0 exacto (lo de antes)."""
    for sim_costs in (True, False):
        executor = TradeExecutor(client=None, live=False, config=_cfg(sim_costs=sim_costs))
        pos = await executor.buy("M", 1.0)
        await executor.sell(pos, 1.0, "test")
        if sim_costs:
            # ~1% + ~2% de ida, ~1% + ~2% de vuelta, más los fees fijos.
            assert -7.0 < pos.realized_pnl_pct < -5.0
            assert pos.market_entry_price == 1.0 and pos.entry_price > 1.0
        else:
            assert pos.realized_pnl_pct == pytest.approx(0.0)
            assert pos.entry_price == 1.0


@pytest.mark.parametrize("campo, valor", [("sim_fee_pct", -1), ("sim_fee_pct", 100),
                                          ("sim_slippage_pct", -0.5)])
def test_valores_invalidos(campo, valor):
    with pytest.raises(ValueError, match=campo):
        validate_config(make_config(**{campo: valor}))
