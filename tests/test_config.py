import pytest

from pepump.config import load_config


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


def test_load_config_unknown_keys_are_ignored_but_warned(tmp_path, capsys):
    path = write_toml(tmp_path, """
[general]
live = false
clave_inventada = 123

[pumpportal]
api_key = "abc"
""")
    cfg = load_config(path)
    assert not hasattr(cfg, "clave_inventada")
    captured = capsys.readouterr()
    assert "clave_inventada" in captured.out
