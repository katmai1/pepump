from dataclasses import dataclass
import os
import tomllib


# ======================================================================
# CONFIGURACIÓN
# ======================================================================

@dataclass
class Config:
    # Se cargan desde config.toml (ver Config.from_toml) — NUNCA hardcodees
    # aquí tu key ni tu RPC real, y NUNCA los pegues en un chat, log o repo.
    pumpportal_api_key: str = ""
    helius_api_key: str = ""
    
    # --- logging / debug ---
    verbose: bool = False
 
    @property
    def pumpportal_ws_url(self) -> str:
        return f"wss://pumpportal.fun/api/data?api-key={self.pumpportal_api_key}"
    
    @property
    def solana_rpc_url(self) -> str:
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
 
    @classmethod
    def from_toml(cls, path: str) -> "Config":
        """Carga la configuración desde un archivo TOML (ver config.example.toml
        como plantilla). El archivo real (config.toml) NUNCA debe subirse a git
        — está en el .gitignore que te dejé."""
        if not os.path.exists(path):
            raise RuntimeError(
                f"No se encontró {path}. Copia config.example.toml a {path}"
                f"y rellena tus valores reales (API key, RPC, etc.)."
            )
        with open(path, "rb") as f:
            data = tomllib.load(f)
 
        # aplana todas las secciones ([credentials], [scanner], [executor]...)
        # en un único dict de kwargs para el dataclass
        flat = {}
        for value in data.values():
            if isinstance(value, dict):
                flat.update(value)
            # (ignora claves sueltas de nivel superior si las hubiera)
        return cls(**flat)
 
    def validate(self):
        """Comprueba que las credenciales están configuradas antes de arrancar,
        para no descubrirlo a medias de una sesión con errores silenciosos."""
        missing = []
        if not self.pumpportal_api_key:
            missing.append("pumpportal_api_key")
        if not self.helius_api_key:
            missing.append("helius_api_key")
        if missing:
            raise RuntimeError(
                f"Faltan valores en config.toml: {', '.join(missing)}."
            )
 
 
def _mask_key(key: str, visible_chars: int = 4) -> str:
    """Nunca imprimas una API key completa. Usa esto si alguna vez necesitas
    mostrarla en un log o mensaje de error para depurar."""
    if not key or len(key) <= visible_chars:
        return "****"
    return f"{key[:visible_chars]}...{'*' * 8}"