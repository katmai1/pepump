"""
Tests para MeteoraDammV2OnChainClient y para cómo OnchainFallback elige
entre Meteora DLMM y DAMM v2.

Caso real: HqmFi8...6Ax8 (WEFT) tiene ~345 SOL en su pool DAMM v2 y pools
DLMM pequeños. Leyendo solo el DLMM más grande (bin_step 200), el precio
se movía a saltos del 2% y se quedaba quieto entre medias.
"""
import asyncio

import pytest
from solders.pubkey import Pubkey

from pepump.jupiter import JUPITER_POOL
from pepump.onchain import fallback as fallback_module
from pepump.onchain import meteora_damm_v2 as damm_module
from pepump.onchain.fallback import OnchainFallback
from pepump.onchain.meteora_damm_v2 import MeteoraDammV2OnChainClient
from pepump.onchain.programs import WSOL_MINT
from tests.test_meteora_dlmm_onchain import (
    FakeAccount,
    FakeKeyedAccount,
    FakeResp,
    mint_account_data,
    token_account_data,
)

MINT = "HqmFi85adLD8BQA7en1HfajdAfzVFps5REgQaYuw6Ax8"
LAMPORTS = 10 ** 9
TOKEN_UNIT = 10 ** 6
# sqrt_price real del pool E5bdE2...scgx (token_a = WEFT, 6 decimales;
# token_b = SOL) -> ~6.9855e-6 SOL/token, en línea con Jupiter.
REAL_SQRT_PRICE = 1541763393907808669


def build_pool_data(token_a_mint: str, token_b_mint: str, vault_a: str, vault_b: str,
                    sqrt_price: int, status: int = 0) -> bytes:
    """Account data crudo de un Pool de Meteora DAMM v2 (1112 bytes)."""
    data = bytearray(1112)
    data[168:200] = bytes(Pubkey.from_string(token_a_mint))
    data[200:232] = bytes(Pubkey.from_string(token_b_mint))
    data[232:264] = bytes(Pubkey.from_string(vault_a))
    data[264:296] = bytes(Pubkey.from_string(vault_b))
    data[456:472] = sqrt_price.to_bytes(16, "little")
    data[481] = status
    return bytes(data)


class FakeDammRpc:
    """Doble de AsyncClient con pools, vaults y la cuenta del mint en
    memoria. Aplica los filtros memcmp igual que el RPC real."""

    def __init__(self, fail: bool = False):
        self.pools = {}
        self.accounts = {MINT: mint_account_data(6)}
        self.fail = fail

    def add_pool(self, sol_is_a: bool, sol_amount: int, token_amount: int, sqrt_price: int,
                 status: int = 0) -> str:
        address, sol_vault, token_vault = (str(Pubkey.new_unique()) for _ in range(3))
        self.accounts[sol_vault] = token_account_data(sol_amount)
        self.accounts[token_vault] = token_account_data(token_amount)
        if sol_is_a:
            data = build_pool_data(WSOL_MINT, MINT, sol_vault, token_vault, sqrt_price, status)
        else:
            data = build_pool_data(MINT, WSOL_MINT, token_vault, sol_vault, sqrt_price, status)
        self.pools[address] = data
        return address

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_program_accounts(self, program_id, encoding=None, filters=None):
        if self.fail:
            raise RuntimeError("RPC caído (fake)")
        return FakeResp([
            FakeKeyedAccount(address, data)
            for address, data in self.pools.items()
            if all(data[f.offset:f.offset + 32] == bytes(Pubkey.from_string(f.bytes)) for f in filters)
        ])

    async def get_account_info(self, pubkey, encoding=None):
        data = self.pools.get(str(pubkey))
        return FakeResp(FakeAccount(data) if data is not None else None)

    async def get_multiple_accounts(self, pubkeys, encoding=None):
        return FakeResp([FakeAccount(self.accounts[str(p)]) if str(p) in self.accounts else None
                         for p in pubkeys])


@pytest.fixture(autouse=True)
def _cache_limpio():
    MeteoraDammV2OnChainClient._best_pool_by_mint.clear()
    yield
    MeteoraDammV2OnChainClient._best_pool_by_mint.clear()


def _fetch(monkeypatch, rpc):
    monkeypatch.setattr(damm_module, "AsyncClient", lambda url, timeout=None: rpc)
    return asyncio.run(MeteoraDammV2OnChainClient("http://fake-rpc").fetch_price_reserve_or_confirm_absent(MINT))


def test_precio_del_sqrt_price_con_datos_reales(monkeypatch):
    rpc = FakeDammRpc()
    rpc.add_pool(False, 345 * LAMPORTS, 49_295_275 * TOKEN_UNIT, REAL_SQRT_PRICE)

    price, sol_reserve, confirmed_absent = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(6.985476e-6, rel=1e-6)
    assert sol_reserve == pytest.approx(345.0)
    assert confirmed_absent is False


def test_sol_como_token_a_invierte_el_precio(monkeypatch):
    rpc = FakeDammRpc()
    # raw = 4 tokens crudos por lamport -> 1/4 * 10^(6-9) SOL/token
    rpc.add_pool(True, 50 * LAMPORTS, 1_000_000 * TOKEN_UNIT, 2 * 2 ** 64)

    price, _sol, _absent = _fetch(monkeypatch, rpc)

    assert price == pytest.approx(0.25e-3)


def test_usa_el_pool_con_mas_sol_y_descarta_relleno_y_deshabilitados(monkeypatch):
    rpc = FakeDammRpc()
    rpc.add_pool(False, 10 * LAMPORTS, 10 * TOKEN_UNIT, 1 * 2 ** 64)
    rpc.add_pool(False, 500 * LAMPORTS, 10 * TOKEN_UNIT, 2 * 2 ** 64, status=1)  # deshabilitado
    rpc.add_pool(False, LAMPORTS // 10, 10 * TOKEN_UNIT, 3 * 2 ** 64)  # relleno
    best = rpc.add_pool(False, 100 * LAMPORTS, 10 * TOKEN_UNIT, 4 * 2 ** 64)

    price, sol_reserve, _absent = _fetch(monkeypatch, rpc)

    assert sol_reserve == pytest.approx(100.0)
    assert price == pytest.approx(16 * 1e-3)
    assert MeteoraDammV2OnChainClient._best_pool_by_mint[MINT] == best


def test_sin_pools_confirma_ausencia(monkeypatch):
    assert _fetch(monkeypatch, FakeDammRpc()) == (None, None, True)


def test_rpc_caido_no_confirma_ausencia(monkeypatch):
    assert _fetch(monkeypatch, FakeDammRpc(fail=True)) == (None, None, False)


# --------------------------------------------------------------------------- #
# Elección entre DLMM y DAMM v2, y por dónde va la orden
# --------------------------------------------------------------------------- #

class FakeMeteora:
    def __init__(self, price, sol, absent=False):
        self.result = (price, sol, absent)

    def __call__(self, rpc_url):
        return self

    async def fetch_price_reserve_or_confirm_absent(self, mint):
        return self.result

    async def fetch_price(self, mint):
        return self.result[0]


def _resolve(monkeypatch, dlmm, damm):
    monkeypatch.setattr(fallback_module, "MeteoraDlmmOnChainClient", dlmm)
    monkeypatch.setattr(fallback_module, "MeteoraDammV2OnChainClient", damm)
    onchain = OnchainFallback(MINT, "http://fake-rpc")
    price, _absent = asyncio.run(onchain._try_meteora())
    return onchain, price


def test_el_caso_real_usa_damm_v2_que_es_donde_esta_la_liquidez(monkeypatch):
    onchain, price = _resolve(monkeypatch, FakeMeteora(7.51e-6, 37.0), FakeMeteora(6.98e-6, 345.0))

    assert onchain.source == "meteora-damm-v2"
    assert price == 6.98e-6


def test_si_el_dlmm_tiene_mas_sol_se_queda_con_el_dlmm(monkeypatch):
    onchain, price = _resolve(monkeypatch, FakeMeteora(7.5e-6, 500.0), FakeMeteora(7.0e-6, 20.0))

    assert onchain.source == "meteora-dlmm"
    assert price == 7.5e-6


def test_ausencia_solo_si_los_dos_la_confirman(monkeypatch):
    monkeypatch.setattr(fallback_module, "MeteoraDlmmOnChainClient", FakeMeteora(None, None, True))
    monkeypatch.setattr(fallback_module, "MeteoraDammV2OnChainClient", FakeMeteora(None, None, False))
    onchain = OnchainFallback(MINT, "http://fake-rpc")

    assert asyncio.run(onchain._try_meteora()) == (None, False)


class _Jupiter:
    def __init__(self, can_sign):
        self.can_sign = can_sign


@pytest.mark.parametrize("jupiter, live, expected", [
    (None, False, "auto"),
    (_Jupiter(can_sign=False), False, JUPITER_POOL),   # simulado: basta con cotizar
    (_Jupiter(can_sign=False), True, "auto"),          # real sin clave: no se puede firmar
    (_Jupiter(can_sign=True), True, JUPITER_POOL),
])
@pytest.mark.parametrize("source", ["meteora-dlmm", "meteora-damm-v2"])
def test_las_ordenes_de_meteora_van_por_jupiter_si_se_puede(source, jupiter, live, expected):
    onchain = OnchainFallback(MINT, "http://fake-rpc", source, jupiter=jupiter, live=live)
    assert onchain.pool_override() == expected
