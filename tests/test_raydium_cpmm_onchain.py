"""
Tests para RaydiumCpmmOnChainClient: precio de referencia de mints que no
son de pump.fun pero operan en Raydium CPMM contra SOL.

Caso real: Dz9mQ9...bonk (bonk.fun graduado) tiene un pool CPMM mint/SOL
con ~22k SOL y, además, varios pools de relleno con centésimas de SOL. Hay
que quedarse con el de verdad, y descontar las comisiones acumuladas que
viven dentro de los vaults.
"""
import asyncio

import pytest
from solders.pubkey import Pubkey

from pepump import pump as pump_module
from pepump.pump import WSOL_MINT, RaydiumCpmmOnChainClient

MINT = "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk"
LAMPORTS = 10 ** 9
TOKEN_UNIT = 10 ** 6


def build_cpmm_pool_data(token_0_mint: str, token_1_mint: str, vault_0: str, vault_1: str,
                         decimals_0: int, decimals_1: int, status: int = 0,
                         protocol_fees_0: int = 0, creator_fees_1: int = 0) -> bytes:
    """Account data crudo de un PoolState de Raydium CPMM (637 bytes)."""
    data = bytearray(637)
    data[72:104] = bytes(Pubkey.from_string(vault_0))
    data[104:136] = bytes(Pubkey.from_string(vault_1))
    data[168:200] = bytes(Pubkey.from_string(token_0_mint))
    data[200:232] = bytes(Pubkey.from_string(token_1_mint))
    data[329] = status
    data[331] = decimals_0
    data[332] = decimals_1
    data[341:349] = protocol_fees_0.to_bytes(8, "little")
    data[405:413] = creator_fees_1.to_bytes(8, "little")
    return bytes(data)


def token_account_data(amount: int) -> bytes:
    return bytes(64) + amount.to_bytes(8, "little") + bytes(93)


class FakeResp:
    def __init__(self, value):
        self.value = value


class FakeAccount:
    def __init__(self, data):
        self.data = data


class FakeKeyedAccount:
    def __init__(self, pubkey: str, data: bytes):
        self.pubkey = Pubkey.from_string(pubkey)
        self.account = FakeAccount(data)


class FakeCpmmRpc:
    """Doble de AsyncClient con pools CPMM y saldos de vaults en memoria.
    Aplica los filtros memcmp igual que el RPC real."""

    def __init__(self, fail: bool = False):
        self.pools = {}
        self.balances = {}
        self.fail = fail
        self.get_program_accounts_calls = 0

    def add_pool(self, sol_is_token_0: bool, sol_amount: int, token_amount: int, status: int = 0,
                 sol_fees: int = 0, token_fees: int = 0) -> str:
        address, sol_vault, token_vault = (str(Pubkey.new_unique()) for _ in range(3))
        self.balances[sol_vault] = sol_amount
        self.balances[token_vault] = token_amount
        if sol_is_token_0:
            data = build_cpmm_pool_data(WSOL_MINT, MINT, sol_vault, token_vault, 9, 6, status,
                                        protocol_fees_0=sol_fees, creator_fees_1=token_fees)
        else:
            data = build_cpmm_pool_data(MINT, WSOL_MINT, token_vault, sol_vault, 6, 9, status,
                                        protocol_fees_0=token_fees, creator_fees_1=sol_fees)
        self.pools[address] = data
        return address

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_program_accounts(self, program_id, encoding=None, filters=None):
        self.get_program_accounts_calls += 1
        if self.fail:
            raise RuntimeError("RPC caído (fake)")
        matches = [
            FakeKeyedAccount(address, data)
            for address, data in self.pools.items()
            if all(data[f.offset:f.offset + 32] == bytes(Pubkey.from_string(f.bytes)) for f in filters)
        ]
        return FakeResp(matches)

    async def get_account_info(self, pubkey, encoding=None):
        data = self.pools.get(str(pubkey))
        return FakeResp(FakeAccount(data) if data is not None else None)

    async def get_multiple_accounts(self, pubkeys, encoding=None):
        return FakeResp([
            FakeAccount(token_account_data(self.balances[str(p)])) if str(p) in self.balances else None
            for p in pubkeys
        ])


@pytest.fixture(autouse=True)
def _cache_limpio():
    RaydiumCpmmOnChainClient._best_pool_by_mint.clear()
    yield
    RaydiumCpmmOnChainClient._best_pool_by_mint.clear()


def _fetch(monkeypatch, rpc):
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: rpc)
    return asyncio.run(RaydiumCpmmOnChainClient("http://fake-rpc").fetch_price_or_confirm_absent(MINT))


def test_elige_el_pool_con_mas_sol_y_descuenta_las_comisiones(monkeypatch):
    rpc = FakeCpmmRpc()
    # 100 SOL - 1 SOL de fees / 50k tokens - 10k de fees -> 99 / 40_000
    rpc.add_pool(True, 100 * LAMPORTS, 50_000 * TOKEN_UNIT, sol_fees=1 * LAMPORTS, token_fees=10_000 * TOKEN_UNIT)
    rpc.add_pool(False, 10 * LAMPORTS, 10_000 * TOKEN_UNIT)
    rpc.add_pool(True, int(0.005 * LAMPORTS), 1 * TOKEN_UNIT)  # relleno, precio absurdo

    price, confirmed_absent = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(99 / 40_000)
    assert confirmed_absent is False


def test_sol_como_token_1(monkeypatch):
    rpc = FakeCpmmRpc()
    rpc.add_pool(False, 50 * LAMPORTS, 25_000 * TOKEN_UNIT, sol_fees=2 * LAMPORTS, token_fees=5_000 * TOKEN_UNIT)

    price, confirmed_absent = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(48 / 20_000)
    assert confirmed_absent is False


def test_solo_pools_de_relleno_confirma_ausencia(monkeypatch):
    rpc = FakeCpmmRpc()
    rpc.add_pool(True, int(0.005 * LAMPORTS), 2 * TOKEN_UNIT)
    rpc.add_pool(False, 2866, 1386)

    assert _fetch(monkeypatch, rpc) == (None, True)


def test_sin_pools_confirma_ausencia(monkeypatch):
    assert _fetch(monkeypatch, FakeCpmmRpc()) == (None, True)


def test_pool_con_swap_deshabilitado_se_ignora(monkeypatch):
    rpc = FakeCpmmRpc()
    rpc.add_pool(True, 1_000 * LAMPORTS, 1_000 * TOKEN_UNIT, status=1 << 2)
    rpc.add_pool(True, 10 * LAMPORTS, 10_000 * TOKEN_UNIT)

    price, _ = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(0.001)


def test_polling_usa_el_pool_cacheado_sin_repetir_getprogramaccounts(monkeypatch):
    rpc = FakeCpmmRpc()
    rpc.add_pool(True, 10 * LAMPORTS, 10_000 * TOKEN_UNIT)
    _fetch(monkeypatch, rpc)
    calls_after_discovery = rpc.get_program_accounts_calls

    sol_vault = next(v for v, amount in rpc.balances.items() if amount == 10 * LAMPORTS)
    rpc.balances[sol_vault] = 20 * LAMPORTS  # el precio se movió
    price, _ = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(0.002)
    assert rpc.get_program_accounts_calls == calls_after_discovery


def test_fallo_de_rpc_no_confirma_ausencia(monkeypatch):
    assert _fetch(monkeypatch, FakeCpmmRpc(fail=True)) == (None, False)
