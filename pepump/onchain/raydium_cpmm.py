"""Precio de un mint desde su pool de Raydium CPMM contra SOL."""
import logging
from typing import ClassVar, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

from pepump.onchain.programs import RAYDIUM_CPMM_PROGRAM_ID, WSOL_MINT
from pepump.onchain.rpc import get_program_accounts, rpc_value

logger = logging.getLogger(__name__)


class RaydiumCpmmOnChainClient:
    """
    Fallback de precio para mints que NO son de pump.fun pero operan en
    Raydium CPMM (CP-Swap) contra SOL -típicamente tokens de bonk.fun/
    LaunchLab ya graduados-. subscribeTokenTrade no entrega trades de
    Raydium, así que sin esto el bot no tenía de dónde sacar el precio.

    Lee el pool DIRECTO de Solana vía RPC. Cualquiera puede crear un pool
    CPMM para cualquier mint (el caso real Dz9mQ9...bonk tiene uno con
    ~22k SOL y varios de relleno con centésimas de SOL), así que se
    descartan los que no son mint/WSOL, los que tienen el swap
    deshabilitado y los que tienen menos de `MIN_SOL_RESERVE` SOL, y de
    los que quedan se usa el de MÁS SOL.

    Layout de PoolState (637 bytes; vaults, mints y decimales verificados
    on-chain contra el pool real de Dz9mQ9...bonk):
      0   discriminador Anchor        (8)
      8   amm_config                  (32)
      40  pool_creator                (32)
      72  token_0_vault               (32)
      104 token_1_vault               (32)
      136 lp_mint                     (32)
      168 token_0_mint                (32)
      200 token_1_mint                (32)
      232 token_0_program / 264 token_1_program / 296 observation_key
      328 auth_bump                   (1)
      329 status                      (1)  bit 2 = swap deshabilitado
      330 lp_mint_decimals            (1)
      331 mint_0_decimals             (1)
      332 mint_1_decimals             (1)
      333 lp_supply                   (u64)
      341 protocol_fees_token_0       (u64)
      349 protocol_fees_token_1       (u64)
      357 fund_fees_token_0           (u64)
      365 fund_fees_token_1           (u64)
      373 open_time / 381 recent_epoch (u64)
      389 creator_fee_on (1) / 390 enable_creator_fee (1) / 391 padding (6)
      397 creator_fees_token_0        (u64)
      405 creator_fees_token_1        (u64)

    Los vaults guardan las reservas MÁS las comisiones acumuladas sin
    retirar: la reserva real es el saldo del vault menos protocol + fund
    + creator fees de ese lado.

    Solo lectura de cuentas públicas; la orden la sigue armando PumpPortal
    (con pool="raydium-cpmm", ver bot.py:_current_pool_override).
    """

    PROGRAM_ID = RAYDIUM_CPMM_PROGRAM_ID
    # Por debajo de esto un pool es de relleno: su "precio" no significa
    # nada y no es contra el que va a rutear la orden.
    MIN_SOL_RESERVE = 1.0
    _SOL_DECIMALS = 9
    _TOKEN_0_VAULT_OFFSET = 72
    _TOKEN_1_VAULT_OFFSET = 104
    _TOKEN_0_MINT_OFFSET = 168
    _TOKEN_1_MINT_OFFSET = 200
    _STATUS_OFFSET = 329
    _MINT_0_DECIMALS_OFFSET = 331
    _MINT_1_DECIMALS_OFFSET = 332
    _FEE_OFFSETS_TOKEN_0 = (341, 357, 397)  # protocol, fund, creator
    _FEE_OFFSETS_TOKEN_1 = (349, 365, 405)
    _MIN_POOL_ACCOUNT_LEN = 373  # hasta fund_fees_token_1 inclusive
    _SWAP_DISABLED_BIT = 1 << 2
    _TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
    _MAX_ACCOUNTS_PER_CALL = 100  # límite de getMultipleAccounts
    # Pool elegido por mint, para no repetir los getProgramAccounts (caros,
    # y los que se comen el rate limit del RPC) en cada ciclo de polling.
    # Se descarta en cuanto el pool deja de servir.
    _best_pool_by_mint: ClassVar[dict] = {}

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def fetch_price(self, mint: str) -> Optional[float]:
        price, _confirmed_absent = await self.fetch_price_or_confirm_absent(mint)
        return price

    async def fetch_price_or_confirm_absent(self, mint: str) -> tuple[Optional[float], bool]:
        """(price, confirmed_absent), mismo contrato que
        PumpSwapOnChainClient.fetch_price_or_confirm_absent:
        confirmed_absent=True SOLO si las consultas respondieron bien y no
        hay ningún pool mint/WSOL utilizable (incluido el caso de que solo
        haya pools de relleno). Cualquier error de RPC/parseo devuelve
        (None, False); nunca lanza ninguna excepción hacia arriba."""
        try:
            async with AsyncClient(self.rpc_url) as client:
                cached = self._best_pool_by_mint.get(mint)
                if cached is not None:
                    resp = await client.get_account_info(Pubkey.from_string(cached), encoding="base64")
                    info = rpc_value(resp, f"el pool {cached}")
                    pool = self._parse_pool(info.data if info else None, mint)
                    if pool is not None:
                        best = await self._best_priced_pool(client, [(cached, pool)])
                        if best is not None:
                            return best[1], False
                    self._best_pool_by_mint.pop(mint, None)

                candidates = await self._find_candidates(client, mint)
                best = await self._best_priced_pool(client, candidates)
                if best is None:
                    logger.debug(f"[On-chain Raydium CPMM] {len(candidates)} pools {mint}/SOL, ninguno "
                                 f"con al menos {self.MIN_SOL_RESERVE:g} SOL de liquidez.")
                    return None, True

                address, price, sol_reserve = best
                self._best_pool_by_mint[mint] = address
                logger.debug(f"[On-chain Raydium CPMM] Pool {address} | {sol_reserve:,.3f} SOL de "
                             f"liquidez (de {len(candidates)} pools {mint}/SOL)")
                return price, False
        except Exception as e:
            logger.warning(f"[On-chain Raydium CPMM] Falló la consulta on-chain para {mint}: {e}")
            return None, False

    async def _find_candidates(self, client: AsyncClient, mint: str) -> list[tuple[str, dict]]:
        """Pools mint/WSOL en los dos órdenes posibles (el programa ordena
        token_0/token_1 por dirección, así que SOL puede quedar de
        cualquier lado). El memcmp doble filtra del lado del RPC los pools
        mint/otro-token, que para un token popular son decenas."""
        candidates = []
        program = Pubkey.from_string(self.PROGRAM_ID)
        for mint_0, mint_1 in ((mint, WSOL_MINT), (WSOL_MINT, mint)):
            cuentas = await get_program_accounts(
                client, self.rpc_url, program,
                [MemcmpOpts(offset=self._TOKEN_0_MINT_OFFSET, bytes=mint_0),
                 MemcmpOpts(offset=self._TOKEN_1_MINT_OFFSET, bytes=mint_1)],
                f"los pools de Raydium CPMM de {mint}",
            )
            for acc in cuentas:
                pool = self._parse_pool(getattr(getattr(acc, "account", None), "data", None), mint)
                if pool is not None:
                    candidates.append((str(acc.pubkey), pool))
        return candidates

    async def _best_priced_pool(self, client: AsyncClient,
                                candidates: list[tuple[str, dict]]) -> Optional[tuple[str, float, float]]:
        """Lee los vaults de todos los candidatos (una sola llamada por
        tanda de 100 cuentas) y devuelve (pool, precio SOL/token, reserva
        SOL) del que tenga más SOL, o None si ninguno llega a
        MIN_SOL_RESERVE."""
        if not candidates:
            return None
        vaults: list[Pubkey] = []
        for _address, pool in candidates:
            vaults.extend((pool["sol_vault"], pool["token_vault"]))
        infos: list = []
        for i in range(0, len(vaults), self._MAX_ACCOUNTS_PER_CALL):
            resp = await client.get_multiple_accounts(vaults[i:i + self._MAX_ACCOUNTS_PER_CALL],
                                                      encoding="base64")
            infos.extend(rpc_value(resp, "los vaults de los pools") or [])

        best = None
        for idx, (address, pool) in enumerate(candidates):
            sol_amount = self._token_account_amount(infos[2 * idx])
            token_amount = self._token_account_amount(infos[2 * idx + 1])
            if sol_amount is None or token_amount is None:
                continue
            sol_reserve = (sol_amount - pool["sol_fees"]) / 10 ** self._SOL_DECIMALS
            token_reserve = (token_amount - pool["token_fees"]) / 10 ** pool["token_decimals"]
            if sol_reserve < self.MIN_SOL_RESERVE or token_reserve <= 0:
                continue
            if best is None or sol_reserve > best[2]:
                best = (address, sol_reserve / token_reserve, sol_reserve)
        return best

    @classmethod
    def _parse_pool(cls, data, mint: str) -> Optional[dict]:
        """Vault, comisiones y decimales de cada lado (SOL/token) del pool,
        o None si la cuenta está cortada, no es un pool mint/WSOL, o tiene
        el swap deshabilitado."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        data = bytes(data)
        mint_0 = str(Pubkey.from_bytes(data[cls._TOKEN_0_MINT_OFFSET:cls._TOKEN_0_MINT_OFFSET + 32]))
        mint_1 = str(Pubkey.from_bytes(data[cls._TOKEN_1_MINT_OFFSET:cls._TOKEN_1_MINT_OFFSET + 32]))
        if (mint_0, mint_1) == (WSOL_MINT, mint):
            sol_side = 0
        elif (mint_0, mint_1) == (mint, WSOL_MINT):
            sol_side = 1
        else:
            return None
        if data[cls._STATUS_OFFSET] & cls._SWAP_DISABLED_BIT:
            return None

        token_side = 1 - sol_side
        vaults = (Pubkey.from_bytes(data[cls._TOKEN_0_VAULT_OFFSET:cls._TOKEN_0_VAULT_OFFSET + 32]),
                  Pubkey.from_bytes(data[cls._TOKEN_1_VAULT_OFFSET:cls._TOKEN_1_VAULT_OFFSET + 32]))
        decimals = (data[cls._MINT_0_DECIMALS_OFFSET], data[cls._MINT_1_DECIMALS_OFFSET])
        fees = (sum(cls._u64(data, off) for off in cls._FEE_OFFSETS_TOKEN_0),
                sum(cls._u64(data, off) for off in cls._FEE_OFFSETS_TOKEN_1))
        return {
            "sol_vault": vaults[sol_side],
            "token_vault": vaults[token_side],
            "sol_fees": fees[sol_side],
            "token_fees": fees[token_side],
            "token_decimals": decimals[token_side],
        }

    @staticmethod
    def _u64(data: bytes, offset: int) -> int:
        """u64 little-endian; 0 si la cuenta no llega a ese offset."""
        if len(data) < offset + 8:
            return 0
        return int.from_bytes(data[offset:offset + 8], "little")

    @classmethod
    def _token_account_amount(cls, info) -> Optional[int]:
        """`amount` de una cuenta SPL token (mismo offset en Token-2022)."""
        data = getattr(info, "data", None)
        if data is None or len(data) < cls._TOKEN_ACCOUNT_AMOUNT_OFFSET + 8:
            return None
        return cls._u64(bytes(data), cls._TOKEN_ACCOUNT_AMOUNT_OFFSET)
