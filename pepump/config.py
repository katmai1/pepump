from dataclasses import dataclass, fields
import tomllib


@dataclass
class AppConfig:
    # --- [general] --------------------------------------------------- #
    mint: str = ""
    live: bool = False
    # Cada cuánto tiempo (segundos) se muestra en pantalla el %% de profit actual.
    status_interval_seconds: float = 5.0

    # --- [trade] ------------------------------------------------------- #
    buy_sol: float = 0.05
    slippage: float = 15.0
    priority_fee: float = 0.00001
    pool: str = "auto"  # "pump", "raydium", "pump-amm", "launchlab", "raydium-cpmm", "bonk", "auto"

    # --- [strategy] ------------------------------------------------------ #
    activation_pct: float = 10.0
    trailing_pct: float = 15.0
    initial_stop_pct: float = 25.0

    # --- [pumpportal] ----------------------------------------------------- #
    api_key: str = ""  # también se puede definir con la variable de entorno PUMPPORTAL_API_KEY


def load_config(path: str) -> AppConfig:
    """Lee el .toml (organizado en secciones [general]/[trade]/[strategy]/
    [pumpportal] solo por legibilidad) y arma UNA única AppConfig con todos
    los campos juntos."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    valid_keys = {f.name for f in fields(AppConfig)}
    merged: dict = {}
    for section_name in ("general", "trade", "strategy", "pumpportal"):
        section = raw.get(section_name, {})
        unknown = set(section) - valid_keys
        if unknown:
            print(f"Aviso: claves desconocidas en el .toml para [{section_name}]: {sorted(unknown)}")
        merged.update({k: v for k, v in section.items() if k in valid_keys})

    config = AppConfig(**merged)

    # Defensivo: un espacio, tab o salto de línea colado al copiar el mint
    # (o la api_key) al .toml no rompe el parseo del TOML en sí, pero hace
    # que el subscribeTokenTrade se acepte igual (PumpPortal no valida que
    # el mint exista) y después nunca matchee ningún trade real -> el bot
    # se queda esperando para siempre en silencio. Lo limpiamos acá.
    config.mint = config.mint.strip()
    config.api_key = config.api_key.strip()

    if not config.mint:
        raise ValueError("Falta 'mint' en la sección [general] del archivo .toml")

    if not config.api_key:
        raise ValueError(
            "Falta la API key de PumpPortal (pumpportal.api_key en el .toml, o la variable de "
            "entorno PUMPPORTAL_API_KEY). Es obligatoria SIEMPRE, aunque general.live = false: "
            "el bot usa únicamente subscribeTokenTrade para el precio, y ese feed requiere API "
            "key + wallet con al menos 0.02 SOL para entregar trades."
        )

    return config