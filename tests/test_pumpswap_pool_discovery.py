"""
Tests para PumpSwapOnChainClient._find_pool: el pool OFICIAL de migración
de un mint de pump.fun en PumpSwap.

BUGFIX (429 Too Many Requests del RPC): antes se buscaba con un
getProgramAccounts sobre todo el programa de PumpSwap -la consulta más
cara de Solana, con un cupo propio muy bajo en los RPC- en cada
comprobación de migración y en cada ciclo de polling. Ahora la dirección
del pool oficial se CALCULA (es un PDA) y basta un getAccountInfo.
"""
import asyncio

import pytest
from solders.pubkey import Pubkey

from pepump.pump import PUMPSWAP_PROGRAM_ID, WSOL_MINT, PumpSwapOnChainClient

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


class FakeAccountInfo:
    def __init__(self, data: bytes, owner: str = PUMPSWAP_PROGRAM_ID):
        self.data = data
        self.owner = Pubkey.from_string(owner)


class FakeResp:
    def __init__(self, value):
        self.value = value


class SpyRpcClient:
    """Solo responde getAccountInfo; cualquier getProgramAccounts revienta
    (es justo lo que ya no se tiene que usar)."""

    def __init__(self, account: FakeAccountInfo = None):
        self.account = account
        self.get_account_info_calls = []

    async def get_account_info(self, pubkey, encoding=None, commitment=None):
        self.get_account_info_calls.append(str(pubkey))
        return FakeResp(self.account)

    async def get_program_accounts(self, *a, **kw):
        raise AssertionError("ya no se usa getProgramAccounts para PumpSwap")


MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _find(account):
    client = SpyRpcClient(account)
    onchain = PumpSwapOnChainClient("http://fake-rpc")
    return asyncio.run(onchain._find_pool(client, MINT)), client


# Pools oficiales REALES de mainnet (leídos de transacciones de PumpSwap y
# comprobados: creator = pool-authority de pump.fun, index 0, quote WSOL).
@pytest.mark.parametrize("mint,pool", [
    ("HzR1r6Dtf5NMdTxtwc9TnT3YtTsi4ho6dSmnwzAFpump", "2fwA8jvv6neYiT15RbzmLDcSFEq13oLegE4WVGYzxycx"),
    ("14qsVuH4ZLX1xprZotj5Q8ShT1FfJXhPVKoa4jK8pump", "HzPzT2G4fo93ztoTxLGhQiyDvJk2Ph6qgX5HWjnFtLnv"),
])
def test_canonical_pool_address_coincide_con_pools_reales(mint, pool):
    assert PumpSwapOnChainClient.canonical_pool_address(mint) == pool


def test_un_solo_getaccountinfo_a_la_direccion_calculada():
    (address, pool), client = _find(FakeAccountInfo(build_pool_account_data(MINT, WSOL_MINT, 1_000)))
    expected = PumpSwapOnChainClient.canonical_pool_address(MINT)
    assert address == expected
    assert pool is not None and pool["quote_mint"] == WSOL_MINT
    assert client.get_account_info_calls == [expected]


def test_si_la_cuenta_no_existe_no_hay_pool():
    """El mint no migró (o nunca fue de pump.fun): confirmación limpia."""
    (address, pool), _ = _find(None)
    assert (address, pool) == (None, None)


def test_sin_pool_confirma_ausencia_en_fetch():
    client = SpyRpcClient(None)

    class _Ctx:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *a):
            return False

    import pepump.pump as pump_module
    original = pump_module.AsyncClient
    pump_module.AsyncClient = lambda url: _Ctx()
    try:
        price, absent = asyncio.run(PumpSwapOnChainClient("http://fake-rpc").fetch_price_or_confirm_absent(MINT))
    finally:
        pump_module.AsyncClient = original
    assert price is None and absent is True


def test_cuenta_de_otro_mint_no_se_usa():
    otro = "So11111111111111111111111111111111111111112"
    (address, pool), _ = _find(FakeAccountInfo(build_pool_account_data(otro, WSOL_MINT, 1_000)))
    assert address is not None and pool is None


def test_cuenta_que_no_es_del_programa_no_se_usa():
    data = build_pool_account_data(MINT, WSOL_MINT, 1_000)
    (address, pool), _ = _find(FakeAccountInfo(data, owner="11111111111111111111111111111111"))
    assert address is not None and pool is None


def test_layout_viejo_sin_coin_creator_se_parsea_igual():
    data = build_pool_account_data(MINT, WSOL_MINT, 1_000, con_coin_creator=False)
    (address, pool), _ = _find(FakeAccountInfo(data))
    assert pool is not None


def test_canonical_pool_creator_coincide_con_el_pda_real():
    """PDA verificado on-chain para Dz9mQ9...bonk."""
    mint = "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk"
    assert (PumpSwapOnChainClient.canonical_pool_creator(mint)
            == "HCbKYZiFNfjTY5UtBBX2ETak9FFnTQ9KE1pGUH2HkTvv")


@pytest.mark.parametrize("lp_supply", [0, 1, 2**63])
def test_lp_supply_se_lee_como_u64_little_endian(lp_supply):
    data = build_pool_account_data(MINT, WSOL_MINT, lp_supply)
    assert PumpSwapOnChainClient._pool_lp_supply(data) == lp_supply


def test_quote_mint_se_lee_del_offset_correcto():
    data = build_pool_account_data(MINT, WSOL_MINT, 1)
    assert PumpSwapOnChainClient._pool_quote_mint(data) == WSOL_MINT
