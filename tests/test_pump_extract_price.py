import pytest

from pepump.pumpportal import PumpPortalClient


def test_extract_price_level1_bonding_curve_reserves():
    event = {"vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_000_000.0}
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(30.0 / 1_000_000.0)


# Trade real de PumpPortal de un token ya en PumpSwap (caso #86 FdKd...pump,
# SIS): sin reservas de la curva. marketCapSol sale de quote_vault/base_vault
# (4.092e-6) y se deja la reserva virtual del pool (precio real 4.355e-6).
_TRADE_PUMPSWAP_SIS = {
    "signature": "3xPrRD4Qsmx1MeVbax59SyFYvoCv8GVX6Y61ah23U1nAKjSHK11W1iCeiWLxfmmAYUaHSL4b5ndgfJndfxZLVWH9",
    "mint": "FdKdpz11GsTofLaNiddrhxrsE4APZYSSfLFqQ9YKpump",
    "txType": "buy",
    "pool": "pump-amm",
    "solAmount": 0.049938,
    "tokenAmount": 11347.431215,
    "marketCapSol": 4092.24,
}


def test_trade_de_token_migrado_no_da_precio():
    """Ni marketCapSol ni solAmount/tokenAmount: los dos ignoran la reserva
    virtual de los pools nuevos de PumpSwap y daban un precio ~6% por
    debajo del real. El precio de un token migrado sale on-chain."""
    assert PumpPortalClient.extract_price(_TRADE_PUMPSWAP_SIS) is None


def test_extract_price_prefers_bonding_curve_over_market_cap():
    event = {
        "vSolInBondingCurve": 30.0,
        "vTokensInBondingCurve": 1_000_000.0,
        "marketCapSol": 999.0,  # no debería usarse: nivel 1 tiene prioridad
    }
    price = PumpPortalClient.extract_price(event)
    assert price == pytest.approx(30.0 / 1_000_000.0)


def test_extract_price_returns_none_when_no_usable_field():
    event = {"message": "Successfully subscribed to keys."}
    assert PumpPortalClient.extract_price(event) is None


def test_extract_price_ignores_zero_token_reserves_in_bonding_curve():
    # v_tok == 0 -> división por cero evitada, sin precio (no 0.0).
    event = {"vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 0, "marketCapSol": 27.0}
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


def test_reservas_en_cero_no_devuelven_cero():
    """Con vSolInBondingCurve en 0 el nivel 1 devolvía 0.0 en vez de None."""
    assert PumpPortalClient.extract_price({
        "vSolInBondingCurve": 0,
        "vTokensInBondingCurve": 1000000,
    }) is None


def test_reservas_ilegibles_no_dan_precio():
    """Reservas que no son números -> None, sin TypeError (y sin caer al
    precio del trade, que en un token migrado es el que está mal)."""
    assert PumpPortalClient.extract_price({
        "vSolInBondingCurve": None,
        "vTokensInBondingCurve": "no-es-un-numero",
        "solAmount": 1.5,
        "tokenAmount": 300.0,
    }) is None


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
