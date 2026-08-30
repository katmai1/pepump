"""
Tests para el mensaje de error MÁS CLARO cuando una compra/venta real
falla on-chain (revert). Antes, el bot solo mostraba el error crudo de
solders (ej. "TransactionErrorInstructionError((3, Tagged(Custom(
InstructionErrorCustom(1)))))"), que no dice nada por sí mismo. Ahora:
  - si el código Custom(N) es un error conocido de spl-token (el CPI
    más común en cualquier ruta de compra/venta, sea pump.fun, PumpSwap,
    Raydium, etc.), se agrega su descripción en texto plano.
  - se pide la transacción completa (getTransaction) para sumar las
    líneas de log del programa que mencionan el error real.
"""
import pytest
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

    monkeypatch.setattr(pump_module, "_fetch_relevant_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(3, InstructionErrorCustom(1))
    short_reason, debug_detail = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", err)

    assert "InsufficientFunds" in short_reason
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

    monkeypatch.setattr(pump_module, "_fetch_relevant_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(3, InstructionErrorCustom(9999))  # código desconocido
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

    monkeypatch.setattr(pump_module, "_fetch_relevant_program_logs", fake_fetch_logs)

    err = TransactionErrorInstructionError(3, InstructionErrorCustom(9999))
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

    monkeypatch.setattr(pump_module, "_fetch_relevant_program_logs", fake_fetch_logs)

    short_reason, debug_detail = await _describe_onchain_error("fake-sig", "https://fake-rpc.test", "error-no-estandar")

    assert "no reconocido" in short_reason
