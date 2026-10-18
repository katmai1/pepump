"""Precio de un mint desde su pool de Meteora DLMM contra SOL."""
import logging
import math
from typing import ClassVar, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

from pepump.onchain.programs import METEORA_DLMM_PROGRAM_ID, WSOL_MINT
from pepump.onchain.rpc import get_program_accounts, rpc_value

logger = logging.getLogger(__name__)


class MeteoraDlmmOnChainClient:
    """
    Fallback de precio para mints cuya liquidez contra SOL está en Meteora
    DLMM. Caso real: Hg5Ja5...pump (baton) completó la bonding curve, pero
    su pool oficial de PumpSwap está cotizado contra PUMP, no contra SOL, y
    la liquidez mint/SOL (~$670k) vive en pools DLMM.

    Lee el LbPair DIRECTO de Solana vía RPC. Igual que en Raydium CPMM,
    cualquiera puede crear pools para cualquier mint (el caso real tiene 7
    pools mint/SOL, varios casi vacíos), así que se descartan los que no
    están habilitados y los que tienen menos de `MIN_SOL_RESERVE` SOL, y de
    los que quedan se usa el de MÁS SOL.

    Layout de LbPair (904 bytes; offsets verificados on-chain contra los
    pools reales 9Ndiy...7s6 y BN7Cf...Gakf de Hg5Ja5...pump):
      0   discriminador Anchor        (8)
      8   parameters (32) / 40 v_parameters (32)
      72  bump_seed (1) / 73 bin_step_seed (2) / 75 pair_type (1)
      76  active_id                   (i32)
      80  bin_step                    (u16, en basis points)
      82  status                      (u8)  0 = habilitado
      83  require_base_factor_seed (1) / 84 base_factor_seed (2)
      86  activation_type (1) / 87 creator_pool_on_off_control (1)
      88  token_x_mint                (32)
      120 token_y_mint                (32)
      152 reserve_x                   (32)
      184 reserve_y                   (32)

    El precio NO sale de las reservas (la liquidez está repartida en bins),
    sino del bin activo: (1 + bin_step/10000) ^ active_id unidades crudas de
    token_y por unidad cruda de token_x. Las reservas solo se leen para
    descartar pools de relleno. El LbPair no guarda decimales, así que se
    leen de la cuenta del mint (offset 44, igual en Token-2022).

    Solo lectura de cuentas públicas. La Lightning API de PumpPortal NO
    tiene un pool "meteora": la orden se manda con pool="auto" (ver
    bot.py:_current_pool_override) y queda en manos de PumpPortal.
    """

    PROGRAM_ID = METEORA_DLMM_PROGRAM_ID
    # Por debajo de esto un pool es de relleno: su "precio" no significa
    # nada y no es contra el que va a rutear la orden.
    MIN_SOL_RESERVE = 1.0
    _SOL_DECIMALS = 9
    _BASIS_POINT_MAX = 10_000
    _ACTIVE_ID_OFFSET = 76
    _BIN_STEP_OFFSET = 80
    _STATUS_OFFSET = 82
    _TOKEN_X_MINT_OFFSET = 88
    _TOKEN_Y_MINT_OFFSET = 120
    _RESERVE_X_OFFSET = 152
    _RESERVE_Y_OFFSET = 184
    _MIN_POOL_ACCOUNT_LEN = 216  # hasta reserve_y inclusive
    _STATUS_ENABLED = 0
    _MINT_DECIMALS_OFFSET = 44
    _TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
    _MAX_ACCOUNTS_PER_CALL = 100  # límite de getMultipleAccounts
    # Pool elegido por mint, para no repetir los getProgramAccounts en
    # cada ciclo de polling. Se descarta en cuanto el pool deja de servir.
    _best_pool_by_mint: ClassVar[dict] = {}

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def fetch_price(self, mint: str) -> Optional[float]:
        price, _confirmed_absent = await self.fetch_price_or_confirm_absent(mint)
        return price

    async def fetch_price_or_confirm_absent(self, mint: str) -> tuple[Optional[float], bool]:
        """(price, confirmed_absent), mismo contrato que
        RaydiumCpmmOnChainClient.fetch_price_or_confirm_absent."""
        try:
            async with AsyncClient(self.rpc_url) as client:
                cached = self._best_pool_by_mint.get(mint)
                if cached is not None:
                    resp = await client.get_account_info(Pubkey.from_string(cached), encoding="base64")
                    info = rpc_value(resp, f"el pool {cached}")
                    pool = self._parse_pool(info.data if info else None, mint)
                    if pool is not None:
                        best = await self._best_priced_pool(client, mint, [(cached, pool)])
                        if best is not None:
                            return best[1], False
                    self._best_pool_by_mint.pop(mint, None)

                candidates = await self._find_candidates(client, mint)
                best = await self._best_priced_pool(client, mint, candidates)
                if best is None:
                    logger.debug(f"[On-chain Meteora DLMM] {len(candidates)} pools {mint}/SOL, ninguno "
                                 f"con al menos {self.MIN_SOL_RESERVE:g} SOL de liquidez.")
                    return None, True

                address, price, sol_reserve = best
                self._best_pool_by_mint[mint] = address
                logger.debug(f"[On-chain Meteora DLMM] Pool {address} | {sol_reserve:,.3f} SOL de "
                             f"liquidez (de {len(candidates)} pools {mint}/SOL)")
                return price, False
        except Exception as e:
            logger.warning(f"[On-chain Meteora DLMM] Falló la consulta on-chain para {mint}: {e}")
            return None, False

    async def _find_candidates(self, client: AsyncClient, mint: str) -> list[tuple[str, dict]]:
        """Pools mint/WSOL en los dos órdenes posibles (x/y), con doble
        memcmp para que el RPC filtre los pools mint/otro-token."""
        candidates = []
        program = Pubkey.from_string(self.PROGRAM_ID)
        for mint_x, mint_y in ((mint, WSOL_MINT), (WSOL_MINT, mint)):
            cuentas = await get_program_accounts(
                client, self.rpc_url, program,
                [MemcmpOpts(offset=self._TOKEN_X_MINT_OFFSET, bytes=mint_x),
                 MemcmpOpts(offset=self._TOKEN_Y_MINT_OFFSET, bytes=mint_y)],
                f"los pools de Meteora DLMM de {mint}",
            )
            for acc in cuentas:
                pool = self._parse_pool(getattr(getattr(acc, "account", None), "data", None), mint)
                if pool is not None:
                    candidates.append((str(acc.pubkey), pool))
        return candidates

    async def _best_priced_pool(self, client: AsyncClient, mint: str,
                                candidates: list[tuple[str, dict]]) -> Optional[tuple[str, float, float]]:
        """Lee los decimales del mint y las reservas de todos los candidatos
        (una sola llamada por tanda de 100 cuentas) y devuelve (pool, precio
        SOL/token, reserva SOL) del que tenga más SOL, o None si ninguno
        llega a MIN_SOL_RESERVE."""
        if not candidates:
            return None
        accounts = [Pubkey.from_string(mint)]
        for _address, pool in candidates:
            accounts.extend((pool["sol_reserve"], pool["token_reserve"]))
        infos: list = []
        for i in range(0, len(accounts), self._MAX_ACCOUNTS_PER_CALL):
            resp = await client.get_multiple_accounts(accounts[i:i + self._MAX_ACCOUNTS_PER_CALL],
                                                      encoding="base64")
            infos.extend(rpc_value(resp, "las reservas de los pools") or [])

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
            # raw_price = token_y crudo por token_x crudo; se lleva a SOL por
            # token entero según de qué lado quedó SOL.
            sol_per_raw_token = 1 / pool["raw_price"] if pool["sol_is_x"] else pool["raw_price"]
            price = sol_per_raw_token * 10 ** (token_decimals - self._SOL_DECIMALS)
            if best is None or sol_reserve > best[2]:
                best = (address, price, sol_reserve)
        return best

    @classmethod
    def _parse_pool(cls, data, mint: str) -> Optional[dict]:
        """Reservas de cada lado (SOL/token) y precio crudo del bin activo,
        o None si la cuenta está cortada, no es un pool mint/WSOL, no está
        habilitado o el precio no es un número usable."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        data = bytes(data)
        mint_x = str(Pubkey.from_bytes(data[cls._TOKEN_X_MINT_OFFSET:cls._TOKEN_X_MINT_OFFSET + 32]))
        mint_y = str(Pubkey.from_bytes(data[cls._TOKEN_Y_MINT_OFFSET:cls._TOKEN_Y_MINT_OFFSET + 32]))
        if (mint_x, mint_y) == (WSOL_MINT, mint):
            sol_is_x = True
        elif (mint_x, mint_y) == (mint, WSOL_MINT):
            sol_is_x = False
        else:
            return None
        if data[cls._STATUS_OFFSET] != cls._STATUS_ENABLED:
            return None

        active_id = int.from_bytes(data[cls._ACTIVE_ID_OFFSET:cls._ACTIVE_ID_OFFSET + 4], "little", signed=True)
        bin_step = int.from_bytes(data[cls._BIN_STEP_OFFSET:cls._BIN_STEP_OFFSET + 2], "little")
        try:
            raw_price = (1 + bin_step / cls._BASIS_POINT_MAX) ** active_id
        except OverflowError:
            return None
        if not math.isfinite(raw_price) or raw_price <= 0:
            return None

        reserves = (Pubkey.from_bytes(data[cls._RESERVE_X_OFFSET:cls._RESERVE_X_OFFSET + 32]),
                    Pubkey.from_bytes(data[cls._RESERVE_Y_OFFSET:cls._RESERVE_Y_OFFSET + 32]))
        return {
            "sol_reserve": reserves[0] if sol_is_x else reserves[1],
            "token_reserve": reserves[1] if sol_is_x else reserves[0],
            "raw_price": raw_price,
            "sol_is_x": sol_is_x,
        }

    @classmethod
    def _token_account_amount(cls, info) -> Optional[int]:
        """`amount` de una cuenta SPL token (mismo offset en Token-2022)."""
        data = getattr(info, "data", None)
        if data is None or len(data) < cls._TOKEN_ACCOUNT_AMOUNT_OFFSET + 8:
            return None
        return int.from_bytes(bytes(data)[cls._TOKEN_ACCOUNT_AMOUNT_OFFSET:cls._TOKEN_ACCOUNT_AMOUNT_OFFSET + 8],
                              "little")
