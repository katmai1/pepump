"""
Tests para MeteoraDlmmOnChainClient: precio de referencia de mints cuya
liquidez contra SOL está en Meteora DLMM.

Caso real: Hg5Ja5...pump (baton) completó la bonding curve, pero su pool
oficial de PumpSwap está cotizado contra PUMP y la liquidez mint/SOL vive
en 7 pools DLMM, varios de ellos casi vacíos.
"""
import asyncio

import pytest
from solders.pubkey import Pubkey

from pepump import pump as pump_module
from pepump.pump import WSOL_MINT, MeteoraDlmmOnChainClient

MINT = "Hg5Ja55T5wESq4vyFoiVCMeHXtGyVA69X2UHq8hgpump"
LAMPORTS = 10 ** 9
TOKEN_UNIT = 10 ** 6


def build_lb_pair_data(token_x_mint: str, token_y_mint: str, reserve_x: str, reserve_y: str,
                       active_id: int, bin_step: int, status: int = 0) -> bytes:
    """Account data crudo de un LbPair de Meteora DLMM (904 bytes)."""
    data = bytearray(904)
    data[76:80] = active_id.to_bytes(4, "little", signed=True)
    data[80:82] = bin_step.to_bytes(2, "little")
    data[82] = status
    data[88:120] = bytes(Pubkey.from_string(token_x_mint))
    data[120:152] = bytes(Pubkey.from_string(token_y_mint))
    data[152:184] = bytes(Pubkey.from_string(reserve_x))
    data[184:216] = bytes(Pubkey.from_string(reserve_y))
    return bytes(data)


def token_account_data(amount: int) -> bytes:
    return bytes(64) + amount.to_bytes(8, "little") + bytes(93)


def mint_account_data(decimals: int) -> bytes:
    return bytes(44) + bytes([decimals]) + bytes(37)


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


class FakeDlmmRpc:
    """Doble de AsyncClient con LbPairs, saldos de reservas y la cuenta del
    mint en memoria. Aplica los filtros memcmp igual que el RPC real."""

    def __init__(self, fail: bool = False, token_decimals: int = 6):
        self.pools = {}
        self.accounts = {MINT: mint_account_data(token_decimals)}
        self.fail = fail
        self.get_program_accounts_calls = 0

    def add_pool(self, sol_is_x: bool, sol_amount: int, token_amount: int,
                 active_id: int, bin_step: int, status: int = 0) -> str:
        address, sol_reserve, token_reserve = (str(Pubkey.new_unique()) for _ in range(3))
        self.accounts[sol_reserve] = token_account_data(sol_amount)
        self.accounts[token_reserve] = token_account_data(token_amount)
        if sol_is_x:
            data = build_lb_pair_data(WSOL_MINT, MINT, sol_reserve, token_reserve, active_id, bin_step, status)
        else:
            data = build_lb_pair_data(MINT, WSOL_MINT, token_reserve, sol_reserve, active_id, bin_step, status)
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
            FakeAccount(self.accounts[str(p)]) if str(p) in self.accounts else None
            for p in pubkeys
        ])


@pytest.fixture(autouse=True)
def _cache_limpio():
    MeteoraDlmmOnChainClient._best_pool_by_mint.clear()
    yield
    MeteoraDlmmOnChainClient._best_pool_by_mint.clear()


def _fetch(monkeypatch, rpc):
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: rpc)
    return asyncio.run(MeteoraDlmmOnChainClient("http://fake-rpc").fetch_price_or_confirm_absent(MINT))


def test_precio_del_bin_activo_con_datos_reales(monkeypatch):
    """Pool real 9Ndiy...7s6: active_id -255, bin_step 100, x = mint (6
    decimales), y = SOL. Leído on-chain: ~7.9077e-5 SOL/token, en línea con
    la cotización de Jupiter para el mismo mint."""
    rpc = FakeDlmmRpc()
    rpc.add_pool(False, 2_000 * LAMPORTS, 25_000_000 * TOKEN_UNIT, active_id=-255, bin_step=100)

    price, confirmed_absent = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(7.907690907e-5)
    assert confirmed_absent is False


def test_sol_como_token_x_invierte_el_precio(monkeypatch):
    rpc = FakeDlmmRpc()
    # raw = 1.01^255 tokens crudos por lamport -> 1 / raw * 10^(6-9) SOL/token
    rpc.add_pool(True, 50 * LAMPORTS, 1_000_000 * TOKEN_UNIT, active_id=255, bin_step=100)

    price, _ = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(1.01 ** -255 * 10 ** -3)


def test_elige_el_pool_con_mas_sol(monkeypatch):
    rpc = FakeDlmmRpc()
    rpc.add_pool(False, 100 * LAMPORTS, 1_000 * TOKEN_UNIT, active_id=-100, bin_step=80)
    rpc.add_pool(False, 900 * LAMPORTS, 1_000 * TOKEN_UNIT, active_id=-200, bin_step=80)
    rpc.add_pool(False, int(0.2 * LAMPORTS), 1 * TOKEN_UNIT, active_id=5_000, bin_step=100)  # relleno

    price, _ = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(1.008 ** -200 * 10 ** -3)


def test_solo_pools_de_relleno_confirma_ausencia(monkeypatch):
    rpc = FakeDlmmRpc()
    rpc.add_pool(False, int(0.3 * LAMPORTS), 10 * TOKEN_UNIT, active_id=-255, bin_step=100)
    rpc.add_pool(True, 2866, 1386, active_id=0, bin_step=25)

    assert _fetch(monkeypatch, rpc) == (None, True)


def test_sin_pools_confirma_ausencia(monkeypatch):
    assert _fetch(monkeypatch, FakeDlmmRpc()) == (None, True)


def test_pool_deshabilitado_se_ignora(monkeypatch):
    rpc = FakeDlmmRpc()
    rpc.add_pool(False, 5_000 * LAMPORTS, 1_000 * TOKEN_UNIT, active_id=0, bin_step=100, status=1)
    rpc.add_pool(False, 10 * LAMPORTS, 1_000 * TOKEN_UNIT, active_id=-255, bin_step=100)

    price, _ = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(1.01 ** -255 * 10 ** -3)


def test_bin_activo_absurdo_no_rompe(monkeypatch):
    rpc = FakeDlmmRpc()
    rpc.add_pool(False, 5_000 * LAMPORTS, 1_000 * TOKEN_UNIT, active_id=2_000_000_000, bin_step=10_000)

    assert _fetch(monkeypatch, rpc) == (None, True)


def test_polling_usa_el_pool_cacheado_sin_repetir_getprogramaccounts(monkeypatch):
    rpc = FakeDlmmRpc()
    address = rpc.add_pool(False, 10 * LAMPORTS, 1_000 * TOKEN_UNIT, active_id=-255, bin_step=100)
    _fetch(monkeypatch, rpc)
    calls_after_discovery = rpc.get_program_accounts_calls

    data = bytearray(rpc.pools[address])
    data[76:80] = (-254).to_bytes(4, "little", signed=True)  # el bin activo se movió
    rpc.pools[address] = bytes(data)
    price, _ = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(1.01 ** -254 * 10 ** -3)
    assert rpc.get_program_accounts_calls == calls_after_discovery


def test_fallo_de_rpc_no_confirma_ausencia(monkeypatch):
    assert _fetch(monkeypatch, FakeDlmmRpc(fail=True)) == (None, False)
