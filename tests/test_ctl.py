"""ctl.py: lo que enseña antes de comprar (sin red)."""
import ctl


def test_avisa_si_entrar_y_salir_cuesta_mucho():
    """Antes ctl.py no enseñaba el coste de entrar y salir: la web avisaba
    a partir del 5% y ctl.py compraba sin decir nada."""
    lineas = ctl.round_trip_lines({"round_trip_pct": -12.0, "buy_sol": 0.05, "transfer_fee_pct": 3.0})
    assert "-12.00%" in lineas[0] and "0.05 SOL" in lineas[0]
    assert lineas[1].startswith("⚠") and "impuesto de transferencia" in lineas[1]


def test_coste_pequeno_se_ensena_sin_aviso():
    assert len(ctl.round_trip_lines({"round_trip_pct": -1.2, "buy_sol": 0.05})) == 1


def test_sin_cotizacion_de_jupiter_no_se_ensena_nada():
    assert ctl.round_trip_lines({"round_trip_pct": None}) == []


def test_abrir_manda_el_coste_de_la_vista_previa():
    body = ctl.open_body({"mint": "M", "round_trip_pct": -0.2}, {"live": True})
    assert body == {"mint": "M", "settings": {"live": True}, "expected_round_trip_pct": -0.2}
    assert "expected_round_trip_pct" not in ctl.open_body({"mint": "M", "round_trip_pct": None}, {})
