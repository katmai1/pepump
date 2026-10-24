import dataclasses
import logging
import os
import pathlib
import tomllib

import pytest

from pepump.config import AppConfig, load_config, validate_config, validate_mint

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_SECTIONS = ("general", "trade", "strategy", "pumpportal", "jupiter", "onchain", "web")
# Campos de AppConfig que NO van en el .toml: `mint` llega por la línea de
# comandos o la web, y `expected_round_trip_pct` de la vista previa de cada
# compra.
FIELDS_NOT_IN_TOML = {"mint", "expected_round_trip_pct"}


@pytest.fixture(autouse=True)
def _sin_claves_del_entorno(monkeypatch):
    """Que una clave exportada en la terminal de quien corre los tests no
    cambie lo que carga el .toml."""
    for var in ("JUPITER_API_KEY", "PEPUMP_WALLET_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)


def write_toml(tmp_path, content: str):
    path = tmp_path / "config.toml"
    path.write_text(content)
    return str(path)


def test_load_config_basic_parses_all_sections(tmp_path):
    path = write_toml(tmp_path, """
[general]
live = true
status_interval_seconds = 7.5

[trade]
buy_sol = 0.1
slippage = 20
priority_fee = 0.0002
pool = "raydium"

[strategy]
activation_pct = 12.0
trailing_pct = 8.0
initial_stop_pct = 30.0

[pumpportal]
api_key = "abc123"
""")
    cfg = load_config(path)

    assert cfg.live is True
    assert cfg.status_interval_seconds == 7.5
    assert cfg.buy_sol == 0.1
    assert cfg.slippage == 20
    assert cfg.priority_fee == 0.0002
    assert cfg.pool == "raydium"
    assert cfg.activation_pct == 12.0
    assert cfg.trailing_pct == 8.0
    assert cfg.initial_stop_pct == 30.0
    assert cfg.api_key == "abc123"


def test_load_config_no_acepta_el_coste_de_la_vista_previa(tmp_path):
    """expected_round_trip_pct es de cada compra (lo pone la vista previa):
    fijado en el .toml compararía todas las compras con el mismo número."""
    path = write_toml(tmp_path, """
[trade]
expected_round_trip_pct = -1.0

[pumpportal]
api_key = "abc123"
""")
    assert load_config(path).expected_round_trip_pct is None


def test_load_config_missing_file_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_config("/no/existe/config.toml")


def test_load_config_malformed_toml_raises_value_error(tmp_path):
    # tomllib.TOMLDecodeError hereda de ValueError.
    path = write_toml(tmp_path, "esto no es toml valido [[[")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_without_api_key_and_without_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PUMPPORTAL_API_KEY", raising=False)
    path = write_toml(tmp_path, "[pumpportal]\napi_key = \"\"\n")
    with pytest.raises(ValueError, match="API key"):
        load_config(path)


def test_load_config_falls_back_to_env_var_when_toml_key_empty(tmp_path, monkeypatch):
    """
    Regresión: el .toml de ejemplo y los mensajes de error siempre
    dijeron que PUMPPORTAL_API_KEY servía como alternativa a escribir la
    api_key en el archivo, pero load_config() nunca leía esa variable de
    entorno -> quien confiara en esa opción documentada se encontraba con
    'Falta la API key' de todas formas. Este test cubre el fix.
    """
    monkeypatch.setenv("PUMPPORTAL_API_KEY", "desde-env-var")
    path = write_toml(tmp_path, "[pumpportal]\napi_key = \"\"\n")
    cfg = load_config(path)
    assert cfg.api_key == "desde-env-var"


def test_load_config_toml_api_key_takes_precedence_over_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPPORTAL_API_KEY", "no-deberia-usarse")
    path = write_toml(tmp_path, "[pumpportal]\napi_key = \"del-archivo\"\n")
    cfg = load_config(path)
    assert cfg.api_key == "del-archivo"


def test_load_config_strips_whitespace_from_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("PUMPPORTAL_API_KEY", raising=False)
    path = write_toml(tmp_path, "[pumpportal]\napi_key = \"  con-espacios  \"\n")
    cfg = load_config(path)
    assert cfg.api_key == "con-espacios"


def test_load_config_unknown_keys_are_ignored_but_warned(tmp_path, caplog):
    path = write_toml(tmp_path, """
[general]
live = false
clave_inventada = 123

[pumpportal]
api_key = "abc"
""")
    cfg = load_config(path)
    assert not hasattr(cfg, "clave_inventada")
    # Se avisa por logging (nunca por print(), ver logging_config.py).
    assert any("clave_inventada" in r.getMessage() for r in caplog.records)
    assert all(r.levelno >= logging.WARNING for r in caplog.records)


# --------------------------------------------------------------------------- #
# config.toml.example <-> AppConfig
# --------------------------------------------------------------------------- #

def _example_keys() -> set:
    raw = tomllib.loads((REPO_ROOT / "config.toml.example").read_text(encoding="utf-8"))
    keys = set()
    for section in CONFIG_SECTIONS:
        keys |= set(raw.get(section, {}))
    return keys


def test_example_toml_documenta_todos_los_campos_de_appconfig():
    """El .example es la única documentación de las opciones: si se agrega
    un campo a AppConfig y no se documenta aquí, nadie se entera de que
    existe (y el default queda enterrado en el código)."""
    declared = {f.name for f in dataclasses.fields(AppConfig)} - FIELDS_NOT_IN_TOML
    missing = sorted(declared - _example_keys())
    assert not missing, f"Faltan en config.toml.example: {missing}"


def test_example_toml_no_tiene_claves_que_ya_no_existen():
    """Al revés: una clave que quedó en el .example después de sacarla de
    AppConfig hace que load_config avise 'clave desconocida' a todo el que
    copie el ejemplo tal cual."""
    declared = {f.name for f in dataclasses.fields(AppConfig)}
    extra = sorted(_example_keys() - declared)
    assert not extra, f"Sobran en config.toml.example: {extra}"


def _toml_con_clave(tmp_path, clave: str, modo: int):
    path = write_toml(tmp_path, f'[pumpportal]\napi_key = "k"\n[jupiter]\nwallet_private_key = "{clave}"\n')
    os.chmod(path, modo)
    return path


@pytest.mark.skipif(os.name != "posix", reason="permisos POSIX")
def test_clave_privada_en_un_toml_legible_por_otros_avisa(tmp_path, caplog):
    path = _toml_con_clave(tmp_path, "secreta", 0o644)
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(path))
    assert cfg.wallet_private_key == "secreta"
    assert "chmod 600" in caplog.text and "secreta" not in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="permisos POSIX")
def test_clave_privada_en_un_toml_protegido_no_avisa(tmp_path, caplog):
    path = _toml_con_clave(tmp_path, "secreta", 0o600)
    with caplog.at_level(logging.WARNING):
        load_config(str(path))
    assert "chmod 600" not in caplog.text


def test_clave_privada_por_variable_de_entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("PEPUMP_WALLET_PRIVATE_KEY", " de-entorno ")
    path = write_toml(tmp_path, '[pumpportal]\napi_key = "k"\n')
    assert load_config(str(path)).wallet_private_key == "de-entorno"


def test_example_toml_carga_sin_errores(tmp_path):
    """El ejemplo tiene que ser un .toml válido y cargable tal cual (con
    una api_key puesta), no solo un archivo de comentarios."""
    text = (REPO_ROOT / "config.toml.example").read_text(encoding="utf-8")
    text = text.replace('\napi_key = ""', '\napi_key = "clave-de-prueba"')
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    cfg = load_config(str(path))

    assert cfg.api_key == "clave-de-prueba"
    # Los valores del ejemplo son los mismos defaults de AppConfig.
    defaults = AppConfig(api_key="clave-de-prueba")
    for field in dataclasses.fields(AppConfig):
        if field.name in FIELDS_NOT_IN_TOML:
            continue
        assert getattr(cfg, field.name) == getattr(defaults, field.name), field.name


# --------------------------------------------------------------------------- #
# validate_mint
# --------------------------------------------------------------------------- #

WSOL = "So11111111111111111111111111111111111111112"


def test_validate_mint_limpia_espacios_y_saltos_de_linea():
    """Un mint copiado con espacios/tabs alrededor pasa el parseo de
    argparse y hasta el subscribeTokenTrade sin error, pero después nunca
    matchea ningún trade: el bot espera para siempre en silencio."""
    assert validate_mint(f"  {WSOL}\n") == WSOL
    assert validate_mint(f"\t{WSOL}  ") == WSOL


@pytest.mark.parametrize("malo", [
    "",
    "   ",
    "no-es-un-mint",
    "0OIl" * 11,                    # base58 no admite 0, O, I, l
    WSOL[:-5],                      # muy corto
    WSOL + "abcdef",                # muy largo
])
def test_validate_mint_rechaza_direcciones_invalidas(malo):
    with pytest.raises(ValueError):
        validate_mint(malo)


def test_validate_mint_mensaje_menciona_el_valor_recibido():
    """El error tiene que apuntar a la causa real. Antes un mint inválido
    reventaba dentro del fallback on-chain y salía como 'no hay ninguna
    fuente de precio disponible', que despista por completo."""
    with pytest.raises(ValueError, match="mint"):
        validate_mint("chirimoya")


# --------------------------------------------------------------------------- #
# Validación de la configuración
# --------------------------------------------------------------------------- #

BASE_TOML = """
[trade]
buy_sol = {buy_sol}
slippage = {slippage}
pool = "{pool}"

[strategy]
activation_pct = {activation_pct}
trailing_pct = {trailing_pct}
initial_stop_pct = {initial_stop_pct}
entry_dip_pct = {entry_dip_pct}
strategy_price_basis = "{strategy_price_basis}"

[pumpportal]
api_key = "abc"

[onchain]
live_feed_timeout_seconds = {live_feed_timeout_seconds}
entry_wait_timeout_seconds = {entry_wait_timeout_seconds}
solana_rpc_url = "{solana_rpc_url}"
"""

DEFAULTS = dict(buy_sol=0.05, slippage=15.0, pool="auto", activation_pct=10.0,
                trailing_pct=15.0, initial_stop_pct=25.0, entry_dip_pct=0.0, strategy_price_basis="real",
                live_feed_timeout_seconds=5.0, entry_wait_timeout_seconds=60.0,
                solana_rpc_url="https://rpc.test")


def write_scenario(tmp_path, **overrides):
    valores = {**DEFAULTS, **overrides}
    return write_toml(tmp_path, BASE_TOML.format(**valores))


def test_config_valida_carga_sin_quejarse(tmp_path):
    cfg = load_config(write_scenario(tmp_path))
    assert cfg.buy_sol == 0.05
    assert cfg.entry_dip_pct == 0.0  # 0 es válido: significa "comprar en la referencia"
    assert cfg.strategy_price_basis == "real"


def test_strategy_price_basis_market_es_valido(tmp_path):
    cfg = load_config(write_scenario(tmp_path, strategy_price_basis="market"))
    assert cfg.strategy_price_basis == "market"


@pytest.mark.parametrize("overrides, fragmento", [
    ({"buy_sol": 0}, "buy_sol"),
    ({"buy_sol": -0.1}, "buy_sol"),
    ({"slippage": 0}, "slippage"),
    ({"pool": "uniswap"}, "pool"),
    ({"activation_pct": 0}, "activation_pct"),
    # 100% de trailing pone el nivel de venta en 0: desarma el stop sin avisar.
    ({"trailing_pct": 100.0}, "trailing_pct"),
    ({"trailing_pct": 0}, "trailing_pct"),
    ({"initial_stop_pct": 100.0}, "initial_stop_pct"),
    # 100% de dip deja el precio objetivo en 0: nunca se llega a comprar.
    ({"entry_dip_pct": 100.0}, "entry_dip_pct"),
    ({"entry_dip_pct": -5.0}, "entry_dip_pct"),
    ({"solana_rpc_url": "rpc.test"}, "solana_rpc_url"),
    ({"strategy_price_basis": "estimado"}, "strategy_price_basis"),
    # El presupuesto total de espera no puede ser menor que un solo ciclo.
    ({"entry_wait_timeout_seconds": 2.0, "live_feed_timeout_seconds": 5.0},
     "entry_wait_timeout_seconds"),
])
def test_config_invalida_falla_con_mensaje_que_nombra_la_clave(tmp_path, overrides, fragmento):
    with pytest.raises(ValueError, match=fragmento):
        load_config(write_scenario(tmp_path, **overrides))


def test_config_junta_todos_los_errores_en_un_solo_mensaje(tmp_path):
    """No sirve arreglar de a un error por corrida: se listan todos."""
    with pytest.raises(ValueError) as exc:
        load_config(write_scenario(tmp_path, buy_sol=0, trailing_pct=150.0, pool="uniswap"))
    mensaje = str(exc.value)
    assert "buy_sol" in mensaje
    assert "trailing_pct" in mensaje
    assert "pool" in mensaje


def test_stall_timeout_corto_avisa_pero_no_falla(tmp_path, caplog):
    """Es raro pero puede ser deliberado, así que arranca de todos modos."""
    path = write_toml(tmp_path, """
[pumpportal]
api_key = "abc"

[onchain]
live_feed_timeout_seconds = 20.0
stall_timeout_seconds = 5.0
""")
    with caplog.at_level(logging.WARNING):
        cfg = load_config(path)
    assert cfg.stall_timeout_seconds == 5.0
    assert any("stall_timeout_seconds" in r.getMessage() for r in caplog.records)


def test_mint_en_el_toml_se_ignora_con_aviso(tmp_path, caplog):
    """El mint viene solo por -m/--mint. Si el .toml lo define, el valor
    se pisa después en run.py: hay que decirlo, no ignorarlo en silencio."""
    path = write_toml(tmp_path, """
[general]
mint = "%s"

[pumpportal]
api_key = "abc"
""" % WSOL)
    with caplog.at_level(logging.WARNING):
        load_config(path)
    assert any("-m/--mint" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Límites de seguridad (max_buy_sol / max_open_positions)
# --------------------------------------------------------------------------- #

def test_buy_sol_no_puede_superar_max_buy_sol():
    with pytest.raises(ValueError, match="max_buy_sol"):
        validate_config(AppConfig(buy_sol=1.0, max_buy_sol=0.5))
    validate_config(AppConfig(buy_sol=0.5, max_buy_sol=0.5))  # igual vale


@pytest.mark.parametrize("valor", [0, -1, 2.5])
def test_max_open_positions_tiene_que_ser_entero_positivo(valor):
    with pytest.raises(ValueError, match="max_open_positions"):
        validate_config(AppConfig(max_open_positions=valor))


@pytest.mark.parametrize("campo", ["buy_sol", "slippage", "activation_pct", "trailing_pct",
                                   "initial_stop_pct", "entry_dip_pct", "priority_fee"])
@pytest.mark.parametrize("valor", [float("nan"), float("inf")])
def test_config_rechaza_valores_no_finitos(campo, valor):
    """NaN pasaba todas las comparaciones: `ctl.py open ... --trailing nan`
    abría una posición que no vendía nunca."""
    with pytest.raises(ValueError, match=campo):
        validate_config(dataclasses.replace(AppConfig(), **{campo: valor}))


def test_limites_por_defecto():
    cfg = AppConfig()
    assert cfg.max_buy_sol == 0.5 and cfg.max_open_positions == 10
