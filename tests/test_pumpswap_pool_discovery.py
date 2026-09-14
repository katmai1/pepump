"""
Tests para PumpSwapOnChainClient._find_pool_address.

BUGFIX: cuando el getProgramAccounts devolvía más de un pool para el mismo
mint, la selección del "mejor" llamaba a fetch_pool_state() por cada
candidato -y esa función hace su PROPIO getAccountInfo. O sea: un
round-trip de RPC extra por candidato, justo en el camino que ya es el que
se come el rate limit del RPC. El account data crudo ya viene en
`acc.account.data` del mismo getProgramAccounts, así que ahora se parsea
localmente, sin llamadas extra.
"""
import asyncio

import pytest
from solders.pubkey import Pubkey

from pepump.pump import WSOL_MINT, PumpSwapOnChainClient

OTRO_QUOTE_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC


def build_pool_account_data(base_mint: str, quote_mint: str, lp_supply: int,
                            con_coin_creator: bool = True, creator: str = None) -> bytes:
    """Arma el account data crudo de un pool de PumpSwap con el mismo
    layout que PumpSwapPoolStateNew/Old de pumpswapamm. Por defecto el
    `creator` es el del pool oficial de migración de pump.fun."""
    relleno32 = bytes(32)
    creator = creator or PumpSwapOnChainClient.canonical_pool_creator(base_mint)
    data = (
        bytes(8)                                    # discriminador Anchor
        + bytes([255])                              # pool_bump
        + (0).to_bytes(2, "little")                 # index
        + bytes(Pubkey.from_string(creator))        # creator
        + bytes(Pubkey.from_string(base_mint))      # base_mint
        + bytes(Pubkey.from_string(quote_mint))     # quote_mint
        + relleno32                                 # lp_mint
        + relleno32                                 # pool_base_token_account
        + relleno32                                 # pool_quote_token_account
        + lp_supply.to_bytes(8, "little")           # lp_supply
    )
    if con_coin_creator:
        data += relleno32                           # coin_creator (layout NEW)
    return data


class FakeAccount:
    def __init__(self, data):
        self.data = data


class FakeKeyedAccount:
    def __init__(self, pubkey: str, data: bytes):
        self.pubkey = Pubkey.from_string(pubkey)
        self.account = FakeAccount(data)


class FakeProgramAccountsResp:
    def __init__(self, value):
        self.value = value


class SpyRpcClient:
    """Cuenta las llamadas de RPC para poder afirmar que la selección de
    candidatos no dispara ninguna de más."""

    def __init__(self, accounts):
        self._accounts = accounts
        self.get_program_accounts_calls = 0
        self.get_account_info_calls = 0

    async def get_program_accounts(self, program_id, encoding=None, filters=None):
        self.get_program_accounts_calls += 1
        return FakeProgramAccountsResp(self._accounts)

    async def get_account_info_json_parsed(self, pubkey, commitment=None):
        self.get_account_info_calls += 1
        raise AssertionError("no debería hacer falta ninguna lectura extra de cuenta")


POOL_A = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
POOL_B = "7YHm6t1LqL9PDkVv8h8ZKmMdMx3oRpQwPMHhz9YR5YQd"
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _find(accounts):
    client = SpyRpcClient(accounts)
    onchain = PumpSwapOnChainClient("http://fake-rpc")
    address = asyncio.run(onchain._find_pool_address(client, MINT))
    return address, client


def test_un_solo_pool_se_devuelve_directo_sin_leer_nada_mas():
    accounts = [FakeKeyedAccount(POOL_A, build_pool_account_data(MINT, WSOL_MINT, 1000))]
    address, client = _find(accounts)

    assert address == POOL_A
    assert client.get_account_info_calls == 0


def test_gana_el_pool_con_mas_lp_supply_sin_round_trips_extra():
    """Antes esto costaba un getAccountInfo por candidato."""
    accounts = [
        FakeKeyedAccount(POOL_A, build_pool_account_data(MINT, WSOL_MINT, 1_000)),
        FakeKeyedAccount(POOL_B, build_pool_account_data(MINT, WSOL_MINT, 9_999_999)),
    ]
    address, client = _find(accounts)

    assert address == POOL_B
    assert client.get_program_accounts_calls == 1
    assert client.get_account_info_calls == 0


def test_se_descartan_los_pools_que_no_estan_denominados_en_sol():
    """El pool de USDC tiene más liquidez, pero el bot solo puede operar
    contra SOL: elegirlo por lp_supply y recién después descubrir que no
    sirve dejaba al bot sin precio teniendo uno disponible."""
    accounts = [
        FakeKeyedAccount(POOL_A, build_pool_account_data(MINT, WSOL_MINT, 1_000)),
        FakeKeyedAccount(POOL_B, build_pool_account_data(MINT, OTRO_QUOTE_MINT, 9_999_999)),
    ]
    address, _ = _find(accounts)

    assert address == POOL_A


def test_layout_viejo_sin_coin_creator_se_parsea_igual():
    """PumpSwapPoolStateOld no trae coin_creator, pero los offsets hasta
    lp_supply son idénticos."""
    accounts = [
        FakeKeyedAccount(POOL_A, build_pool_account_data(MINT, WSOL_MINT, 10, con_coin_creator=False)),
        FakeKeyedAccount(POOL_B, build_pool_account_data(MINT, WSOL_MINT, 20, con_coin_creator=False)),
    ]
    address, _ = _find(accounts)

    assert address == POOL_B


def test_cuenta_mas_corta_de_lo_esperado_no_rompe_la_seleccion():
    """Si un layout futuro deja una cuenta más corta, no se puede leer su
    lp_supply -pero eso no debe tirar excepción ni descartar a los
    candidatos que sí se pueden leer."""
    accounts = [
        FakeKeyedAccount(POOL_A, bytes(40)),  # ilegible
        FakeKeyedAccount(POOL_B, build_pool_account_data(MINT, WSOL_MINT, 500)),
    ]
    address, _ = _find(accounts)

    assert address == POOL_B


WALLET_CUALQUIERA = "9UqBvwTW1WU3e5TTEtSqY7SBcRgN89qZeCiMH8ag1JR7"


def test_se_ignoran_los_pools_que_no_son_el_oficial_de_migracion():
    """Caso real (Dz9mQ9...bonk, token de bonk.fun): un pool de PumpSwap
    creado a mano por una wallet cualquiera daba un precio inventado y la
    Lightning API rechazaba la compra con "Pool account not found"."""
    accounts = [
        FakeKeyedAccount(POOL_A, build_pool_account_data(MINT, WSOL_MINT, 100, creator=WALLET_CUALQUIERA)),
    ]
    address, _ = _find(accounts)

    assert address is None


def test_el_pool_oficial_gana_aunque_haya_uno_no_oficial_con_mas_liquidez():
    accounts = [
        FakeKeyedAccount(POOL_A, build_pool_account_data(MINT, WSOL_MINT, 9_999_999, creator=WALLET_CUALQUIERA)),
        FakeKeyedAccount(POOL_B, build_pool_account_data(MINT, WSOL_MINT, 1_000)),
    ]
    address, _ = _find(accounts)

    assert address == POOL_B


def test_canonical_pool_creator_coincide_con_el_pda_real():
    """PDA verificado on-chain para Dz9mQ9...bonk."""
    mint = "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk"
    assert (PumpSwapOnChainClient.canonical_pool_creator(mint)
            == "HCbKYZiFNfjTY5UtBBX2ETak9FFnTQ9KE1pGUH2HkTvv")


def test_sin_pools_devuelve_none():
    address, _ = _find([])
    assert address is None


@pytest.mark.parametrize("lp_supply", [0, 1, 2**63])
def test_lp_supply_se_lee_como_u64_little_endian(lp_supply):
    data = build_pool_account_data(MINT, WSOL_MINT, lp_supply)
    assert PumpSwapOnChainClient._pool_lp_supply(data) == lp_supply


def test_quote_mint_se_lee_del_offset_correcto():
    data = build_pool_account_data(MINT, WSOL_MINT, 1)
    assert PumpSwapOnChainClient._pool_quote_mint(data) == WSOL_MINT
