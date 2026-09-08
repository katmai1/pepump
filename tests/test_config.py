import dataclasses
import logging
import pathlib
import tomllib

import pytest

from pepump.config import AppConfig, load_config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_SECTIONS = ("general", "trade", "strategy", "pumpportal", "onchain")
# `mint` es el único campo de AppConfig que NO va en el .toml: siempre
# llega por la línea de comandos (-m/--mint, required en run.py).
FIELDS_NOT_IN_TOML = {"mint"}


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
    un campo a AppConfig y no se documenta acá, nadie se entera de que
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


def test_example_toml_carga_sin_errores(tmp_path):
    """El ejemplo tiene que ser un .toml válido y cargable tal cual (con
    una api_key puesta), no solo un archivo de comentarios."""
    text = (REPO_ROOT / "config.toml.example").read_text(encoding="utf-8")
    text = text.replace('api_key = ""', 'api_key = "clave-de-prueba"')
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
