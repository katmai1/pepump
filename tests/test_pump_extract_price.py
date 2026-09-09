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


# --------------------------------------------------------------------------- #
# BUGFIX: el nivel 1 (reservas de la bonding curve) no tenía guardas
# --------------------------------------------------------------------------- #

def test_reservas_como_string_no_tiran_typeerror():
    """Un evento con las reservas serializadas como string reventaba con
    TypeError, y esa excepción subía hasta el `except Exception` de
    _consume_trade_stream -que la logueaba como 'conexión interrumpida' y
    disparaba una reconexión con backoff que no hacía ninguna falta."""
    price = PumpPortalClient.extract_price({
        "vSolInBondingCurve": "30.5",
        "vTokensInBondingCurve": "1000000",
    })
    assert price == pytest.approx(30.5 / 1000000)


def test_reservas_en_cero_caen_al_market_cap_en_vez_de_devolver_cero():
    """Con vSolInBondingCurve en 0 el nivel 1 devolvía 0.0, que el
    llamador descarta como 'sin precio' -perdiendo el marketCapSol del
    mismo evento, que sí servía."""
    price = PumpPortalClient.extract_price({
        "vSolInBondingCurve": 0,
        "vTokensInBondingCurve": 1000000,
        "marketCapSol": 50.0,
    })
    assert price == pytest.approx(50.0 / PumpPortalClient.TOTAL_SUPPLY_TOKENS)


def test_reservas_ilegibles_caen_al_precio_efectivo_del_trade():
    """Nivel 1 y 2 inutilizables -> nivel 3 (solAmount/tokenAmount)."""
    price = PumpPortalClient.extract_price({
        "vSolInBondingCurve": None,
        "vTokensInBondingCurve": "no-es-un-numero",
        "marketCapSol": "tampoco",
        "solAmount": 1.5,
        "tokenAmount": 300.0,
    })
    assert price == pytest.approx(1.5 / 300.0)


@pytest.mark.parametrize("valor", [float("nan"), float("inf"), -1.0, 0])
def test_valores_no_finitos_o_no_positivos_nunca_producen_precio(valor):
    """Un NaN o un inf colado en un precio es peor que no tener precio:
    se propaga a highest_price y desarma el trailing-stop en silencio."""
    assert PumpPortalClient.extract_price({
        "vSolInBondingCurve": valor,
        "vTokensInBondingCurve": valor,
        "marketCapSol": valor,
        "solAmount": valor,
        "tokenAmount": valor,
    }) is None
