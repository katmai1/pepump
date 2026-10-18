"""Precio de un mint desde su pool de Meteora DAMM v2 (CP-AMM) contra SOL."""
import logging
from typing import ClassVar, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

from pepump.onchain.programs import METEORA_DAMM_V2_PROGRAM_ID, WSOL_MINT
from pepump.onchain.rpc import get_program_accounts, rpc_value

logger = logging.getLogger(__name__)


class MeteoraDammV2OnChainClient:
    """
    Fallback de precio para mints cuya liquidez contra SOL está en Meteora
    DAMM v2 -donde acaban, p. ej., los tokens que se gradúan de la Dynamic
    Bonding Curve de Meteora-. Caso real: HqmFi8...6Ax8 (WEFT) tiene ~345 SOL
    en su pool DAMM v2 y solo unos pocos en sus pools DLMM. Leer el DLMM daba
    un precio que solo se movía a saltos de un bin (2% con bin_step 200) y se
    quedaba quieto entre medias, porque ahí casi no se opera.

    Igual que en Meteora DLMM, cualquiera puede crear pools, así que se
    descartan los deshabilitados y los de menos de `MIN_SOL_RESERVE` SOL, y
    de los que quedan se usa el de MÁS SOL.

    Layout de Pool (1112 bytes; offsets verificados on-chain contra el pool
    real E5bdE2...scgx de HqmFi8...6Ax8):
      0   discriminador Anchor        (8)
      8   pool_fees                   (160)
      168 token_a_mint                (32)
      200 token_b_mint                (32)
      232 token_a_vault               (32)
      264 token_b_vault               (32)
      296 whitelisted_vault (32) / 328 partner (32)
      360 liquidity (u128) / 376 padding (u128)
      392 protocol/partner fees       (4 x u64)
      424 sqrt_min_price (u128) / 440 sqrt_max_price (u128)
      456 sqrt_price                  (u128, Q64.64)
      472 activation_point (u64) / 480 activation_type (u8)
      481 pool_status                 (u8)  0 = habilitado

    Precio = (sqrt_price / 2^64)^2 unidades crudas de token_b por unidad
    cruda de token_a. Es el precio del pool SIN comisión: algunos pools
    graduados tienen comisiones altas al principio, que este precio no
    incluye (la orden real sí las paga).

    Solo lectura de cuentas públicas.
    """

    PROGRAM_ID = METEORA_DAMM_V2_PROGRAM_ID
    # Por debajo de esto un pool es de relleno: su "precio" no significa nada.
    MIN_SOL_RESERVE = 1.0
    _SOL_DECIMALS = 9
    _TOKEN_A_MINT_OFFSET = 168
    _TOKEN_B_MINT_OFFSET = 200
    _TOKEN_A_VAULT_OFFSET = 232
    _TOKEN_B_VAULT_OFFSET = 264
    _SQRT_PRICE_OFFSET = 456
    _POOL_STATUS_OFFSET = 481
    _MIN_POOL_ACCOUNT_LEN = 482  # hasta pool_status inclusive
    _STATUS_ENABLED = 0
    _MINT_DECIMALS_OFFSET = 44
    _TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
    _MAX_ACCOUNTS_PER_CALL = 100  # límite de getMultipleAccounts
    # getProgramAccounts sobre DAMM v2 recorre un programa enorme: tarda
    # 5-11 s (medido con Helius), más que los 10 s por defecto de solana-py.
    # Una vez elegido el pool, el polling es un getAccountInfo barato.
    _RPC_TIMEOUT_SECONDS = 30.0
    # Pool elegido por mint, para no repetir los getProgramAccounts en
    # cada ciclo de polling. Se descarta en cuanto el pool deja de servir.
    _best_pool_by_mint: ClassVar[dict] = {}

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def fetch_price(self, mint: str) -> Optional[float]:
        price, _sol_reserve, _confirmed_absent = await self.fetch_price_reserve_or_confirm_absent(mint)
        return price

    async def fetch_price_or_confirm_absent(self, mint: str) -> tuple[Optional[float], bool]:
        price, _sol_reserve, confirmed_absent = await self.fetch_price_reserve_or_confirm_absent(mint)
        return price, confirmed_absent

    async def fetch_price_reserve_or_confirm_absent(
            self, mint: str) -> tuple[Optional[float], Optional[float], bool]:
        """(price, sol_reserve, confirmed_absent): el contrato de
        fetch_price_or_confirm_absent más los SOL del pool elegido, para
        poder compararlo con el mejor pool de Meteora DLMM."""
        try:
            async with AsyncClient(self.rpc_url, timeout=self._RPC_TIMEOUT_SECONDS) as client:
                cached = self._best_pool_by_mint.get(mint)
                if cached is not None:
                    resp = await client.get_account_info(Pubkey.from_string(cached), encoding="base64")
                    info = rpc_value(resp, f"el pool {cached}")
                    pool = self._parse_pool(info.data if info else None, mint)
                    if pool is not None:
                        best = await self._best_priced_pool(client, mint, [(cached, pool)])
                        if best is not None:
                            return best[1], best[2], False
                    self._best_pool_by_mint.pop(mint, None)

                candidates = await self._find_candidates(client, mint)
                best = await self._best_priced_pool(client, mint, candidates)
                if best is None:
                    logger.debug(f"[On-chain Meteora DAMM v2] {len(candidates)} pools {mint}/SOL, ninguno "
                                 f"con al menos {self.MIN_SOL_RESERVE:g} SOL de liquidez.")
                    return None, None, True

                address, price, sol_reserve = best
                self._best_pool_by_mint[mint] = address
                logger.debug(f"[On-chain Meteora DAMM v2] Pool {address} | {sol_reserve:,.3f} SOL de "
                             f"liquidez (de {len(candidates)} pools {mint}/SOL)")
                return price, sol_reserve, False
        except Exception as e:
            logger.warning(f"[On-chain Meteora DAMM v2] Falló la consulta on-chain para {mint}: "
                           f"{type(e).__name__}: {e}")
            return None, None, False

    async def _find_candidates(self, client: AsyncClient, mint: str) -> list[tuple[str, dict]]:
        """Pools mint/WSOL en los dos órdenes posibles (a/b), con doble
        memcmp para que el RPC filtre los pools mint/otro-token."""
        candidates = []
        program = Pubkey.from_string(self.PROGRAM_ID)
        for mint_a, mint_b in ((mint, WSOL_MINT), (WSOL_MINT, mint)):
            cuentas = await get_program_accounts(
                client, self.rpc_url, program,
                [MemcmpOpts(offset=self._TOKEN_A_MINT_OFFSET, bytes=mint_a),
                 MemcmpOpts(offset=self._TOKEN_B_MINT_OFFSET, bytes=mint_b)],
                f"los pools de Meteora DAMM v2 de {mint}",
            )
            for acc in cuentas:
                pool = self._parse_pool(getattr(getattr(acc, "account", None), "data", None), mint)
                if pool is not None:
                    candidates.append((str(acc.pubkey), pool))
        return candidates

    async def _best_priced_pool(self, client: AsyncClient, mint: str,
                                candidates: list[tuple[str, dict]]) -> Optional[tuple[str, float, float]]:
        """Lee los decimales del mint y los vaults de todos los candidatos y
        devuelve (pool, precio SOL/token, reserva SOL) del que tenga más SOL,
        o None si ninguno llega a MIN_SOL_RESERVE."""
        if not candidates:
            return None
        accounts = [Pubkey.from_string(mint)]
        for _address, pool in candidates:
            accounts.extend((pool["sol_vault"], pool["token_vault"]))
        infos: list = []
        for i in range(0, len(accounts), self._MAX_ACCOUNTS_PER_CALL):
            resp = await client.get_multiple_accounts(accounts[i:i + self._MAX_ACCOUNTS_PER_CALL],
                                                      encoding="base64")
            infos.extend(rpc_value(resp, "los vaults de los pools") or [])

        mint_data = getattr(infos[0], "data", None)
        if mint_data is None or len(mint_data) <= self._MINT_DECIMALS_OFFSET:
            return None
        token_decimals = bytes(mint_data)[self._MINT_DECIMALS_OFFSET]

        best = None
        for idx, (address, pool) in enumerate(candidates):
            sol_amount = self._token_account_amount(infos[1 + 2 * idx])
            token_amount = self._token_account_amount(infos[2 + 2 * idx])
            if sol_amount is None or not token_amount:
                continue
            sol_reserve = sol_amount / 10 ** self._SOL_DECIMALS
            if sol_reserve < self.MIN_SOL_RESERVE:
                continue
            # raw_price = token_b crudo por token_a crudo; se lleva a SOL por
            # token entero según de qué lado quedó SOL.
            sol_per_raw_token = 1 / pool["raw_price"] if pool["sol_is_a"] else pool["raw_price"]
            price = sol_per_raw_token * 10 ** (token_decimals - self._SOL_DECIMALS)
            if best is None or sol_reserve > best[2]:
                best = (address, price, sol_reserve)
        return best

    @classmethod
    def _parse_pool(cls, data, mint: str) -> Optional[dict]:
        """Vaults de cada lado (SOL/token) y precio crudo, o None si la
        cuenta está cortada, no es un pool mint/WSOL, no está habilitado o
        el precio es 0."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        data = bytes(data)
        mint_a = str(Pubkey.from_bytes(data[cls._TOKEN_A_MINT_OFFSET:cls._TOKEN_A_MINT_OFFSET + 32]))
        mint_b = str(Pubkey.from_bytes(data[cls._TOKEN_B_MINT_OFFSET:cls._TOKEN_B_MINT_OFFSET + 32]))
        if (mint_a, mint_b) == (WSOL_MINT, mint):
            sol_is_a = True
        elif (mint_a, mint_b) == (mint, WSOL_MINT):
            sol_is_a = False
        else:
            return None
        if data[cls._POOL_STATUS_OFFSET] != cls._STATUS_ENABLED:
            return None

        sqrt_price = int.from_bytes(data[cls._SQRT_PRICE_OFFSET:cls._SQRT_PRICE_OFFSET + 16], "little")
        if sqrt_price <= 0:
            return None
        raw_price = (sqrt_price / 2 ** 64) ** 2

        vaults = (Pubkey.from_bytes(data[cls._TOKEN_A_VAULT_OFFSET:cls._TOKEN_A_VAULT_OFFSET + 32]),
                  Pubkey.from_bytes(data[cls._TOKEN_B_VAULT_OFFSET:cls._TOKEN_B_VAULT_OFFSET + 32]))
        return {
            "sol_vault": vaults[0] if sol_is_a else vaults[1],
            "token_vault": vaults[1] if sol_is_a else vaults[0],
            "raw_price": raw_price,
            "sol_is_a": sol_is_a,
        }

    @classmethod
    def _token_account_amount(cls, info) -> Optional[int]:
        """`amount` de una cuenta SPL token (mismo offset en Token-2022)."""
        data = getattr(info, "data", None)
        if data is None or len(data) < cls._TOKEN_ACCOUNT_AMOUNT_OFFSET + 8:
            return None
        return int.from_bytes(bytes(data)[cls._TOKEN_ACCOUNT_AMOUNT_OFFSET:cls._TOKEN_ACCOUNT_AMOUNT_OFFSET + 8],
                              "little")
