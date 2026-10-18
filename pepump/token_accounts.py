"""Cierre de las cuentas de token vacías de la wallet, para recuperar su rent.

Ni la Lightning API de PumpPortal ni Jupiter cierran la cuenta del token al
venderlo todo (comprobado on-chain: la cuenta queda abierta, con saldo 0 y
~0.0015-0.002 SOL de rent dentro). En una compra de 0.02 SOL eso es un 7%,
y el historial lo registraba como pérdida aunque la operación fuera buena.

Hace falta la clave privada de la wallet (wallet_private_key): el cierre es
una transacción propia, firmada aquí. Solo se cierran cuentas con saldo 0, y
el rent vuelve a la propia wallet.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts, TxOpts
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import CloseAccountParams, close_account

from pepump.jupiter import load_keypair
from pepump.pumpportal import PumpPortalClient

logger = logging.getLogger(__name__)

_LAMPORTS_PER_SOL = 1_000_000_000
# Fee base de una tx con una firma (sin priority fee: el cierre no tiene prisa).
_TX_FEE_LAMPORTS = 5_000


@dataclass
class EmptyTokenAccount:
    address: Pubkey
    program_id: Pubkey
    mint: str
    lamports: int


class TokenAccountCloser:
    # Cierres por transacción: cada uno es una instrucción pequeña, y así
    # una wallet con muchas cuentas vacías no paga una tx por cuenta.
    MAX_CLOSES_PER_TX = 20

    def __init__(self, keypair: Keypair, rpc_url: str, confirm_timeout_seconds: float = 30.0,
                 confirm_poll_interval_seconds: float = 2.0):
        self.keypair = keypair
        self.rpc_url = rpc_url
        self.confirm_timeout_seconds = confirm_timeout_seconds
        self.confirm_poll_interval_seconds = confirm_poll_interval_seconds

    async def find_empty(self, mint: Optional[str] = None) -> list[EmptyTokenAccount]:
        """Cuentas de token de la wallet con saldo 0 (de `mint`, o todas) que
        se pueden cerrar. Se saltan las congeladas y las de Token-2022 con
        comisiones de transferencia retenidas: el programa no deja cerrarlas."""
        vacias, _con_saldo = await self._scan(mint)
        return vacias

    async def _scan(self, mint: Optional[str]) -> tuple[list[EmptyTokenAccount], int]:
        """(cuentas vacías cerrables, cuentas con saldo) de `mint`, o de todos."""
        owner = self.keypair.pubkey()
        filtros = ([TokenAccountOpts(mint=Pubkey.from_string(mint))] if mint else
                   [TokenAccountOpts(program_id=TOKEN_PROGRAM_ID), TokenAccountOpts(program_id=TOKEN_2022_PROGRAM_ID)])
        vacias, con_saldo = [], 0
        async with AsyncClient(self.rpc_url) as client:
            for opts in filtros:
                resp = await client.get_token_accounts_by_owner_json_parsed(owner, opts, commitment=Confirmed)
                for acc in resp.value:
                    info: Any = acc.account.data.parsed["info"]
                    if str(info["tokenAmount"].get("amount")) != "0":
                        con_saldo += 1
                        continue
                    if info.get("state") == "frozen":
                        continue
                    if _withheld_fees(info) > 0:
                        logger.debug(f"[Cuentas de token] {acc.pubkey} tiene comisiones retenidas; no se "
                                     f"puede cerrar.")
                        continue
                    vacias.append(EmptyTokenAccount(acc.pubkey, acc.account.owner, info["mint"],
                                                    acc.account.lamports))
        return vacias, con_saldo

    async def close_empty(self, mint: Optional[str] = None) -> tuple[float, int, int]:
        """Cierra las cuentas vacías (de `mint`, o todas). Devuelve (SOL
        recuperados netos del fee de las tx, cuentas cerradas, cuentas que no
        se pudieron cerrar). Una cuenta que no se deja cerrar no impide cerrar
        las demás; el motivo va al log en DEBUG y quien llama decide si avisa."""
        return await self._close_accounts(await self.find_empty(mint))

    async def close_after_sale(self, mint: str, route_mints=()) -> tuple[float, int, int, bool]:
        """Tras vender todo `mint`: cierra su cuenta y las de los tokens
        intermedios de la ruta (`route_mints`) que hayan quedado vacías.
        Devuelve (recuperado, cerradas, fallidas, pendiente): `pendiente` es
        True si la cuenta de `mint` todavía tiene saldo para el nodo (aún no
        ve la venta), y entonces conviene reintentar."""
        vacias, con_saldo = await self._scan(mint)
        for otro in sorted(set(route_mints) - {mint}):
            # Sus cuentas con saldo (p. ej. USDC propios) no se tocan.
            vacias.extend((await self._scan(otro))[0])
        recuperado, cerradas, fallidas = await self._close_accounts(vacias)
        return recuperado, cerradas, fallidas, con_saldo > 0

    async def _close_accounts(self, cuentas: list[EmptyTokenAccount]) -> tuple[float, int, int]:
        recuperado, cerradas, fallidas = 0, 0, 0
        for i in range(0, len(cuentas), self.MAX_CLOSES_PER_TX):
            lote = cuentas[i:i + self.MAX_CLOSES_PER_TX]
            try:
                await self._close(lote)
            except Exception as e:
                if len(lote) == 1:
                    logger.debug(f"[Cuentas de token] No se pudo cerrar {lote[0].address} "
                                 f"({lote[0].mint}): {e}")
                    fallidas += 1
                    continue
                # Una cuenta que no se deja cerrar hace fallar el lote entero
                # (en la simulación, sin pagar nada): de una en una.
                logger.debug(f"[Cuentas de token] Falló el lote de {len(lote)} ({e}); de una en una.")
                for cuenta in lote:
                    try:
                        await self._close([cuenta])
                    except Exception as e2:
                        logger.debug(f"[Cuentas de token] No se pudo cerrar {cuenta.address} "
                                     f"({cuenta.mint}): {e2}")
                        fallidas += 1
                        continue
                    recuperado += cuenta.lamports - _TX_FEE_LAMPORTS
                    cerradas += 1
                continue
            recuperado += sum(c.lamports for c in lote) - _TX_FEE_LAMPORTS
            cerradas += len(lote)
        return recuperado / _LAMPORTS_PER_SOL, cerradas, fallidas

    async def _close(self, cuentas: list[EmptyTokenAccount]) -> str:
        owner = self.keypair.pubkey()
        instrucciones = [close_account(CloseAccountParams(program_id=c.program_id, account=c.address,
                                                          dest=owner, owner=owner)) for c in cuentas]
        async with AsyncClient(self.rpc_url) as client:
            blockhash = (await client.get_latest_blockhash()).value.blockhash
            tx = Transaction([self.keypair], Message.new_with_blockhash(instrucciones, owner, blockhash), blockhash)
            # Con simulación previa: si algo no se puede cerrar, falla aquí
            # sin pagar el fee. Contra "confirmed", no el "finalized" por
            # defecto de solana-py: va ~13 s por detrás y, justo después de
            # una venta, la cuenta todavía tenía los tokens ahí (caso real:
            # NonNativeHasBalance, 0xb, al cerrar tras vender).
            resp = await client.send_raw_transaction(
                bytes(tx), opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed))
        signature = str(resp.value)
        await PumpPortalClient._confirm_transaction_onchain(
            signature, self.rpc_url, self.confirm_timeout_seconds, self.confirm_poll_interval_seconds)
        return signature


def _withheld_fees(info: dict) -> int:
    for ext in info.get("extensions") or []:
        if ext.get("extension") == "transferFeeAmount":
            try:
                return int((ext.get("state") or {}).get("withheldAmount") or 0)
            except (TypeError, ValueError):
                return 1
    return 0


def build_token_account_closer(config) -> Optional[TokenAccountCloser]:
    """TokenAccountCloser si hay wallet_private_key y close_empty_token_accounts;
    si no, None. ValueError si la clave no es válida o no es de
    pumpportal.wallet_address."""
    if not config.wallet_private_key or not config.close_empty_token_accounts:
        return None
    keypair = load_keypair(config.wallet_private_key)
    if config.wallet_address and str(keypair.pubkey()) != config.wallet_address:
        raise ValueError(f"wallet_private_key es la clave de la wallet {keypair.pubkey()}, pero "
                         f"pumpportal.wallet_address es {config.wallet_address}. Tienen que ser la misma.")
    return TokenAccountCloser(keypair, config.solana_rpc_url, config.tx_confirm_timeout_seconds,
                              config.tx_confirm_poll_interval_seconds)


async def sweep_empty_token_accounts(closer: TokenAccountCloser) -> None:
    """Al arrancar: cierra las cuentas vacías que dejaron operaciones
    anteriores. Nunca lanza."""
    try:
        recuperado, cerradas, fallidas = await closer.close_empty()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[Cuentas de token] No se pudieron revisar las cuentas vacías de la wallet: {e}")
        return
    if cerradas:
        logger.info(f"🧹 Cerradas {cerradas} cuentas de token vacías de operaciones anteriores: "
                    f"recuperados {recuperado:.6f} SOL de rent.")
    if fallidas:
        logger.warning(f"[Cuentas de token] {fallidas} cuentas vacías no se pudieron cerrar (ejecuta "
                       f"con -v para ver el motivo).")
