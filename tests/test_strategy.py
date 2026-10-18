"""TrailingStrategy sola, sin bot ni mocks: la regla de entrada/salida."""
import pytest

from pepump.executor import Position
from pepump.strategy import Action, TrailingStrategy

S = TrailingStrategy(activation_pct=10.0, trailing_pct=15.0, initial_stop_pct=25.0, price_basis="market")


def pos(entry=1.0, market=None, armed=False, highest=None):
    p = Position(mint="M", entry_price=entry, sol_amount=0.05, token_amount=0.05 / entry,
                 market_entry_price=market)
    p.armed = armed
    if highest is not None:
        p.highest_price = highest
    return p


@pytest.mark.parametrize("price, action", [
    (1.05, Action.HOLD),
    (1.10, Action.ARM),       # justo en la activación
    (0.80, Action.HOLD),
    (0.75, Action.SELL),      # justo en el stop-loss inicial
])
def test_sin_armar(price, action):
    assert S.evaluate(pos(), price).action is action


@pytest.mark.parametrize("price, action", [
    (2.10, Action.NEW_HIGH),
    (2.00, Action.HOLD),      # igual al máximo
    (1.71, Action.HOLD),
    (1.70, Action.SELL),      # 15% por debajo de 2.0
])
def test_armado(price, action):
    assert S.evaluate(pos(armed=True, highest=2.0), price).action is action


def test_venta_trae_el_motivo():
    assert "stop-loss inicial" in S.evaluate(pos(), 0.5).reason
    assert "retroceso de 15.0%" in S.evaluate(pos(armed=True, highest=2.0), 1.0).reason


def test_posicion_cerrada_no_hace_nada():
    p = pos()
    p.closed = True
    assert S.evaluate(p, 0.1).action is Action.HOLD


def test_basis_real_mide_contra_el_precio_pagado():
    # Pagaste 1.04 (con comisiones) por un precio de mercado de 1.0.
    p = pos(entry=1.04, market=1.0)
    real = TrailingStrategy(10.0, 15.0, 25.0, "real")
    assert real.activation_price(p) == pytest.approx(1.144)
    assert S.activation_price(p) == pytest.approx(1.10)
    assert real.evaluate(p, 1.12).action is Action.HOLD
    assert S.evaluate(p, 1.12).action is Action.ARM


def test_trailing_stop_price_solo_armado():
    assert S.trailing_stop_price(pos()) is None
    assert S.trailing_stop_price(pos(armed=True, highest=2.0)) == pytest.approx(1.7)
