"""
Tapar secretos (API keys, clave privada, contraseña del panel) en todo lo
que sale del proceso: logs de la terminal, logs del panel web y los
motivos que se guardan en pepump.db.

Hace falta porque varias claves viajan DENTRO de URLs -la Lightning API de
PumpPortal en `?api-key=`, muchos RPC (Helius) igual o en la ruta- y los
mensajes de error de requests/httpx incluyen la URL entera. Caso real
(comprobado): un corte de conexión con la Lightning API daba "Max retries
exceeded with url: /api/trade?api-key=<la clave>", que se logueaba y
quedaba a la vista en el panel web, que por defecto no pide contraseña.
"""
import logging
import re
from typing import List
from urllib.parse import parse_qsl, urlsplit

MASK = "***"

# `api-key=...` / `api_key=...` / `apikey=...` en cualquier URL, aunque la
# clave no esté registrada (p. ej. la de un RPC que se cambió en caliente).
_API_KEY_PARAM = re.compile(r"(api[-_]?key=)[^&\s'\"<>)]+", re.IGNORECASE)
# Más cortos que esto no se tapan literalmente: taparían trozos de texto normal.
_MIN_SECRET_LEN = 8
# Tramos de la ruta de una URL de RPC que parecen un token (QuickNode,
# Alchemy... lo llevan en la ruta, no en la query).
_MIN_PATH_TOKEN_LEN = 16

_secrets: List[str] = []


def register_secret(secret: str) -> None:
    secret = (secret or "").strip()
    if len(secret) >= _MIN_SECRET_LEN and secret not in _secrets:
        _secrets.append(secret)
        # Los largos primero: si uno contiene a otro, se tapa entero.
        _secrets.sort(key=len, reverse=True)


def register_config_secrets(config) -> None:
    """Registra los secretos de la config, incluidos los que van dentro de
    la URL del RPC."""
    for name in ("api_key", "jupiter_api_key", "wallet_private_key", "web_password"):
        register_secret(getattr(config, name, "") or "")
    url = urlsplit(str(getattr(config, "solana_rpc_url", "") or ""))
    for _key, value in parse_qsl(url.query):
        register_secret(value)
    for part in url.path.split("/"):
        if len(part) >= _MIN_PATH_TOKEN_LEN:
            register_secret(part)


def redact(text: str) -> str:
    if not text:
        return text
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    return _API_KEY_PARAM.sub(rf"\g<1>{MASK}", text)


class SecretRedactingFilter(logging.Filter):
    """Va en cada HANDLER (como MintContextFilter): tapa los secretos del
    mensaje y del traceback antes de que se escriban o se manden a la web."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        limpio = redact(message)
        if limpio != message:
            record.msg, record.args = limpio, None
        if record.exc_info and not record.exc_text:
            # El Formatter reutiliza exc_text si ya está: así el traceback
            # sale tapado también.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True
