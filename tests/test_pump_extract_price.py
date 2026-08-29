import pytest

from pepump.pump import PumpPortalClient


def test_extract_price_level1_bonding_curve_reserves():
    event = {"vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_000_000.0}
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(30.0 / 1_000_000.0)


def test_extract_price_level2_market_cap_sol_when_no_bonding_curve():
    event = {"marketCapSol": 27.0}
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(27.0 / PumpPortalClient.TOTAL_SUPPLY_TOKENS)


def test_extract_price_prefers_bonding_curve_over_market_cap():
    event = {
        "vSolInBondingCurve": 30.0,
        "vTokensInBondingCurve": 1_000_000.0,
        "marketCapSol": 999.0,  # no debería usarse: nivel 1 tiene prioridad
    }
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(30.0 / 1_000_000.0)


def test_extract_price_level3_falls_back_to_single_trade_amounts():
    event = {"solAmount": 0.5, "tokenAmount": 1000.0}
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(0.5 / 1000.0)


def test_extract_price_returns_none_when_no_usable_field():
    event = {"message": "Successfully subscribed to keys."}
    assert PumpPortalClient.extract_price(event) is None


def test_extract_price_ignores_zero_token_reserves_in_bonding_curve():
    # v_tok == 0 -> división por cero evitada, debe caer al siguiente nivel.
    event = {"vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 0, "marketCapSol": 27.0}
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(27.0 / PumpPortalClient.TOTAL_SUPPLY_TOKENS)


def test_extract_price_handles_non_numeric_market_cap_gracefully():
    event = {"marketCapSol": "no-es-un-numero", "solAmount": 0.5, "tokenAmount": 1000.0}
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(0.5 / 1000.0)


def test_extract_price_handles_zero_token_amount_in_level3():
    event = {"solAmount": 0.5, "tokenAmount": 0}
    assert PumpPortalClient.extract_price(event) is None
