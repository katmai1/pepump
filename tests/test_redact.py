"""
Secretos tapados en todo lo que sale del proceso (ver redact.py). Caso
real: un corte de conexión con la Lightning API dejaba en el log -y en el
panel web- "Max retries exceeded with url: /api/trade?api-key=<clave>".
"""
import logging
from types import SimpleNamespace

from pepump.bot import TrailingTakeProfitBot
from pepump.redact import MASK, SecretRedactingFilter, redact, register_config_secrets, register_secret
from pepump.web.logbuffer import LogBuffer

from .conftest import FakeTradeStreamClient, SpyExecutor, make_config

MENSAJE_REQUESTS = ("HTTPSConnectionPool(host='pumpportal.fun', port=443): Max retries exceeded with url: "
                    "/api/trade?api-key=k3yS1nRegistrar99 (Caused by NewConnectionError(...))")


def test_tapa_el_parametro_api_key_aunque_no_este_registrado():
    limpio = redact(MENSAJE_REQUESTS)
    assert "k3yS1nRegistrar99" not in limpio
    assert f"api-key={MASK}" in limpio and "pumpportal.fun" in limpio


def test_tapa_los_secretos_de_la_config_y_los_del_rpc():
    register_config_secrets(SimpleNamespace(
        api_key="pp-clave-registrada-1", jupiter_api_key="jup-clave-registrada-2",
        wallet_private_key="5PrivadaDeLaWalletMuyLarga", web_password="contrasenaweb",
        solana_rpc_url="https://mainnet.helius-rpc.com/?api-key=helius-token-33&x=1"))
    register_config_secrets(SimpleNamespace(solana_rpc_url="https://x.quiknode.pro/abcdef0123456789abcdef/"))
    texto = ("pp-clave-registrada-1 jup-clave-registrada-2 5PrivadaDeLaWalletMuyLarga contrasenaweb "
             "'https://mainnet.helius-rpc.com/?api-key=helius-token-33' https://x.quiknode.pro/abcdef0123456789abcdef/")
    limpio = redact(texto)
    for secreto in ("pp-clave", "jup-clave", "5Privada", "contrasenaweb", "helius-token-33", "abcdef0123456789"):
        assert secreto not in limpio


def test_secretos_cortos_no_se_tapan_literalmente():
    register_secret("abc")
    assert redact("abc de toda la vida") == "abc de toda la vida"


def test_el_filtro_tapa_mensaje_args_y_traceback_en_el_panel_web():
    register_secret("clave-del-traceback-77")
    buffer = LogBuffer()
    logger = logging.getLogger("pepump.test_redact")
    logger.addHandler(buffer)
    try:
        try:
            raise RuntimeError("fallo con clave-del-traceback-77")
        except RuntimeError:
            logger.exception("Error al vender: %s", MENSAJE_REQUESTS)
    finally:
        logger.removeHandler(buffer)
    message = buffer.entries()[-1]["message"]
    assert "k3yS1nRegistrar99" not in message and "clave-del-traceback-77" not in message
    assert "Traceback" in message


def test_el_filtro_deja_igual_lo_que_no_tiene_secretos():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "precio %s", (1.5,), None)
    SecretRedactingFilter().filter(record)
    assert record.getMessage() == "precio 1.5"


def test_end_reason_va_tapado_a_la_web_y_a_la_base_de_datos():
    bot = TrailingTakeProfitBot(client=FakeTradeStreamClient(), executor=SpyExecutor(), config=make_config())
    bot.end_reason = f"la compra falló: {MENSAJE_REQUESTS}"
    assert "k3yS1nRegistrar99" not in bot.end_reason
    assert "k3yS1nRegistrar99" not in str(bot.snapshot()["end_reason"])
