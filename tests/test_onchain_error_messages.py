"""
Tests para el mensaje de error MÁS CLARO cuando una compra/venta real
falla on-chain (revert). Antes, el bot solo mostraba el error crudo de
solders (ej. "TransactionErrorInstructionError((3, Tagged(Custom(
InstructionErrorCustom(1)))))"), que no dice nada por sí mismo. Ahora:
  - se identifica QUÉ programa revirtió (línea "Program <id> failed" de
    los logs) y el código Custom(N) se traduce contra la tabla de ESE
    programa: el mismo número significa cosas distintas en pump.fun, en
    PumpSwap y en spl-token (ver onchain_errors.py).
  - se pide la transacción completa (getTransaction) para sumar las
    líneas de log del programa que mencionan el error real.
"""
from solders.transaction_status import InstructionErrorCustom, TransactionErrorInstructionError

from pepump.pump import _describe_onchain_error, _extract_instruction_error


def test_extract_instruction_error_decodifica_custom():
    err = TransactionErrorInstructionError(3, InstructionErrorCustom(1))
    result = _extract_instruction_error(err)
    assert result == (3, 1)


def test_extract_instruction_error_devuelve_none_si_no_matchea():
    assert _extract_instruction_error("cualquier otra cosa") is None
    assert _extract_instruction_error(None) is None


async def test_describe_onchain_error_incluye_causa_conocida_de_spl_token(monkeypatch):
    """Custom(1) es InsufficientFunds en spl-token -el caso real que
    reportó el usuario- debe aparecer traducido en texto plano, en la
    razón CORTA (la que va directo en el mensaje de error principal)."""
    from pepump import pump as pump_module

    async def fake_fetch_logs(signature, rpc_url):
        return []  # simulamos que no se pudieron obtener logs, igual debe decodificar el código

    monkeypatch.setattr(pump_module, "_fetch_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(3, InstructionErrorCustom(1))
    short_reason, debug_detail = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", err)

    assert "InsufficientFunds" in short_reason
    assert "probablemente" in short_reason  # sin logs no se pudo confirmar el programa
    assert "instrucción #3" in short_reason
    # El detalle técnico (error crudo) queda aparte, para el log de debug.
    assert "error crudo" in debug_detail


async def test_describe_onchain_error_incluye_logs_del_programa(monkeypatch):
    """Si no hay código de spl-token conocido pero SÍ hay logs
    relevantes, la razón corta debe usar la línea de log más específica
    (la última) en vez de quedarse con un código pelado."""
    from pepump import pump as pump_module

    async def fake_fetch_logs(signature, rpc_url):
        return [
            "Program log: Instruction: Buy",
            "Program log: Error: slippage tolerance exceeded",
        ]

    monkeypatch.setattr(pump_module, "_fetch_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(3, InstructionErrorCustom(42))  # código desconocido
    short_reason, debug_detail = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", err)

    assert "slippage tolerance exceeded" in short_reason
    assert "logs relevantes del programa" in debug_detail


async def test_describe_onchain_error_mensaje_generico_si_no_hay_nada_util(monkeypatch):
    """Si ni el código es de spl-token conocido ni hay logs, la razón
    corta debe ser un mensaje genérico -pero seguir siendo una frase
    legible, no un objeto crudo."""
    from pepump import pump as pump_module

    async def fake_fetch_logs(signature, rpc_url):
        return []

    monkeypatch.setattr(pump_module, "_fetch_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(3, InstructionErrorCustom(42))
    short_reason, debug_detail = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", err)

    assert "no reconocido" in short_reason
    assert "error crudo" in debug_detail


async def test_describe_onchain_error_no_rompe_si_todo_falla(monkeypatch):
    """Si tanto la decodificación como la obtención de logs fallan (ej.
    err no tiene la forma esperada, RPC caído), no debe tirar excepción
    -debe caer en la razón genérica."""
    from pepump import pump as pump_module

    async def fake_fetch_logs(signature, rpc_url):
        return []

    monkeypatch.setattr(pump_module, "_fetch_program_logs", fake_fetch_logs)

    short_reason, debug_detail = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", "error-no-estandar")

    assert "no reconocido" in short_reason


# --------------------------------------------------------------------------- #
# El mismo código, distinto programa (el bug que motivó onchain_errors.py)
# --------------------------------------------------------------------------- #

PUMPFUN_FAILED_LOGS = [
    "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
    "Program log: Instruction: Buy",
    "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P failed: custom program error: 0x1775",
]

PUMPSWAP_FAILED_LOGS = [
    "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA invoke [1]",
    "Program log: Instruction: Buy",
    "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA failed: custom program error: 0x1774",
]


async def test_6005_de_pumpfun_se_traduce_como_bonding_curve_complete(monkeypatch):
    """6005 en el programa de pump.fun es BondingCurveComplete -el error
    que aparece cuando el ruteo del pool quedó desalineado con la
    migración. Antes caía en el genérico 'error no reconocido' porque
    solo se consultaba la tabla de spl-token."""
    from pepump import pump as pump_module

    async def fake_fetch_logs(signature, rpc_url):
        return PUMPFUN_FAILED_LOGS

    monkeypatch.setattr(pump_module, "_fetch_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(2, InstructionErrorCustom(6005))
    short_reason, debug_detail = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", err)

    assert "BondingCurveComplete" in short_reason
    assert "pump.fun" in short_reason
    assert "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in debug_detail


async def test_mismo_codigo_en_otro_programa_da_otro_mensaje(monkeypatch):
    """6004 es MintDoesNotMatchBondingCurve en pump.fun pero
    ExceededSlippage en PumpSwap. Traducirlo contra la tabla equivocada
    da un diagnóstico directamente falso, así que el programa que revirtió
    tiene que salir de los logs, no de una suposición."""
    from pepump import pump as pump_module

    async def fake_fetch_logs(signature, rpc_url):
        return PUMPSWAP_FAILED_LOGS

    monkeypatch.setattr(pump_module, "_fetch_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(2, InstructionErrorCustom(6004))
    short_reason, _ = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", err)

    assert "ExceededSlippage" in short_reason
    assert "PumpSwap" in short_reason
    assert "BondingCurve" not in short_reason


async def test_codigo_anchor_sin_programa_identificable_no_inventa_un_nombre(monkeypatch):
    """Si no se pudo saber qué programa falló, un código >= 6000 NO debe
    traducirse contra ninguna tabla: sería adivinar. Debe decir que no se
    pudo atribuir."""
    from pepump import pump as pump_module

    async def fake_fetch_logs(signature, rpc_url):
        return ["Program log: Instruction: Buy"]  # sin línea "failed"

    monkeypatch.setattr(pump_module, "_fetch_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(2, InstructionErrorCustom(6005))
    short_reason, _ = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", err)

    assert "BondingCurveComplete" not in short_reason
    assert "no pude identificar" in short_reason
