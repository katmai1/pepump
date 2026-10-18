"""Cliente de Jupiter (Swap API V2, /order + /execute): precio y órdenes
para tokens que no cotizan contra SOL en ningún sitio que el bot sepa leer
(p. ej. los de StonkFun, con la curva de Raydium LaunchLab contra STONK, ORE
o TSLAx). Jupiter encuentra solo la ruta SOL -> quote -> token y la hace en
una única transacción.

A diferencia de la Lightning API de PumpPortal, Jupiter NO firma: /order
devuelve la transacción sin firmar y hay que firmarla aquí con la clave
privada de la wallet (wallet_private_key en [jupiter], o la variable de
entorno PEPUMP_WALLET_PRIVATE_KEY). /execute la envía y espera a que entre.

Plan gratuito de Jupiter: una petición cada ~2 s para TODA la cuenta (las
consultas de precio y /order comparten ese cupo; /execute va aparte). Por
eso todas las llamadas pasan por un único limitador (`_PriorityLimiter`) en
el que las órdenes adelantan a las consultas de precio: con varias
posiciones abiertas, el precio de cada una se refresca más despacio, pero
una venta nunca espera detrás de una consulta.
"""
import asyncio
import base64
import heapq
import itertools
import json
import logging
import time
from typing import Any, Optional

import requests
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from pepump.onchain.programs import WSOL_MINT
from pepump.onchain.rpc import rpc_value
from pepump.onchain.txinfo import fetch_actual_fill
from pepump.pumpportal import TradeOutcomeUnknown

logger = logging.getLogger(__name__)

# Valor de `pool` con el que el executor sabe que la orden va por Jupiter y
# no por la Lightning API (ver OnchainFallback.pool_override).
JUPITER_POOL = "jupiter"

# Prioridades del limitador: menor = antes.
PRIORITY_TRADE = 0
PRIORITY_PRICE = 1
# Consultas de fondo que pueden esperar (registro tras la venta, vista previa).
PRIORITY_BACKGROUND = 2

_LAMPORTS_PER_SOL = 1_000_000_000
# Offset del campo `decimals` en la cuenta de un mint (igual en Token-2022).
_MINT_DECIMALS_OFFSET = 44
# Códigos de /execute con los que la tx seguro que NO entró: Jupiter dejó
# de intentarlo (blockhash vencido) o ni siquiera la aceptó.
_EXECUTE_NOT_LANDED = {-1, -2, -3, -1000, -1002, -1003, -1004, -1005, -2000, -2002, -2003, -2004}


class JupiterError(RuntimeError):
    """Jupiter respondió con un error (sin ruta, petición inválida, límite
    de peticiones agotado...). Nunca significa que se haya enviado nada."""


def load_keypair(secret: str) -> Keypair:
    """Clave privada en base58 (la que exportan Phantom, Solflare o
    PumpPortal) o como lista JSON de 64 bytes (formato de solana-keygen).
    ValueError con un mensaje claro si no es ninguna de las dos (sin
    incluir la clave en el mensaje)."""
    limpio = (secret or "").strip()
    if not limpio:
        raise ValueError("La clave privada está vacía.")
    try:
        # Keypair.from_base58_string revienta con un panic de Rust (que no
        # es Exception) ante un carácter inválido: se decodifica aquí.
        raw = bytes(json.loads(limpio)) if limpio.startswith("[") else _b58decode(limpio)
        return Keypair.from_bytes(raw)
    except Exception:
        raise ValueError("La clave privada no es válida: se espera base58 (como la exporta Phantom "
                         "o PumpPortal) o una lista JSON de 64 números.") from None


class _PriorityLimiter:
    """Como mucho una petición cada `interval` segundos, en orden de
    prioridad (y de llegada dentro de la misma prioridad). Sin tareas de
    fondo: cada llamador espera su turno con sleeps cortos, así funciona
    igual sea cual sea el loop de asyncio que lo use."""

    _POLL_SECONDS = 0.05

    def __init__(self, interval: float):
        self.interval = interval
        self._next_at = 0.0
        self._queue: list = []
        self._seq = itertools.count()

    def penalize(self, seconds: float) -> None:
        """Retrasa la próxima petición (p. ej. tras un 429 con Retry-After)."""
        self._next_at = max(self._next_at, time.monotonic() + seconds)

    async def acquire(self, priority: int) -> None:
        ticket = (priority, next(self._seq))
        heapq.heappush(self._queue, ticket)
        try:
            while True:
                now = time.monotonic()
                if self._queue[0] == ticket and now >= self._next_at:
                    heapq.heappop(self._queue)
                    self._next_at = now + self.interval
                    return
                # Se vuelve a mirar cada poco aunque falte mucho: puede
                # llegar alguien con más prioridad que tenga que ir antes.
                await asyncio.sleep(self._POLL_SECONDS)
        except BaseException:
            if ticket in self._queue:
                self._queue.remove(ticket)
                heapq.heapify(self._queue)
            raise


class JupiterClient:
    BASE_URL = "https://api.jup.ag/swap/v2"
    HTTP_TIMEOUT_SECONDS = 10
    # Reintentos de una petición de ORDEN que vuelve con 429. Las de precio
    # no se reintentan: ya se pedirá otra en el próximo ciclo.
    TRADE_RATE_LIMIT_RETRIES = 3

    def __init__(self, api_key: str, keypair: Optional[Keypair] = None,
                 request_interval_seconds: float = 2.0):
        self.api_key = api_key
        self.keypair = keypair
        self._limiter = _PriorityLimiter(request_interval_seconds)
        self._decimals: dict = {}

    @property
    def can_sign(self) -> bool:
        return self.keypair is not None

    @property
    def wallet_address(self) -> Optional[str]:
        return str(self.keypair.pubkey()) if self.keypair is not None else None

    # ---- HTTP ------------------------------------------------------------- #

    def _headers(self) -> dict:
        # Con la cabecera vacía Jupiter contesta 401; sin ella, cupo anónimo.
        return {"x-api-key": self.api_key} if self.api_key else {}

    async def _get(self, path: str, params: dict, priority: int) -> dict:
        attempts = self.TRADE_RATE_LIMIT_RETRIES if priority == PRIORITY_TRADE else 1
        for attempt in range(1, attempts + 1):
            await self._limiter.acquire(priority)
            resp = await asyncio.to_thread(
                requests.get, f"{self.BASE_URL}{path}", params=params,
                headers=self._headers(), timeout=self.HTTP_TIMEOUT_SECONDS,
            )
            if resp.status_code != 429:
                break
            retry_after = _retry_after_seconds(resp)
            self._limiter.penalize(retry_after)
            if attempt == attempts:
                raise JupiterError(f"Jupiter: límite de peticiones agotado (429) en {path}.")
            logger.debug(f"[Jupiter] 429 en {path}; reintento en {retry_after:.1f}s.")
        if resp.status_code != 200:
            raise JupiterError(f"Jupiter devolvió {resp.status_code} en {path}: {resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise JupiterError(f"Respuesta inesperada de Jupiter en {path}: {data!r}")
        return data

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int,
                    priority: int = PRIORITY_PRICE) -> dict:
        """/order SIN taker: solo la cotización. JupiterError si no hay ruta."""
        data = await self._get("/order", {"inputMint": input_mint, "outputMint": output_mint,
                                          "amount": str(amount_raw)}, priority)
        if _as_int(data.get("outAmount")) <= 0:
            motivo = data.get("errorMessage") or data.get("error") or "sin ruta"
            raise JupiterError(f"Jupiter no encontró ruta {input_mint} -> {output_mint}: {motivo}")
        return data

    # ---- Precio ----------------------------------------------------------- #

    async def token_decimals(self, mint: str, rpc_url: str) -> int:
        if mint not in self._decimals:
            async with AsyncClient(rpc_url) as client:
                resp = await client.get_account_info(Pubkey.from_string(mint))
            account = rpc_value(resp, f"la cuenta del mint {mint}")
            if account is None or len(bytes(account.data)) <= _MINT_DECIMALS_OFFSET:
                raise JupiterError(f"No se pudo leer la cuenta del mint {mint}.")
            self._decimals[mint] = bytes(account.data)[_MINT_DECIMALS_OFFSET]
        return self._decimals[mint]

    async def buy_tokens(self, mint: str, sol: float, rpc_url: str,
                         priority: int = PRIORITY_PRICE) -> float:
        """Tokens que darían ahora mismo por `sol` SOL (ya con la ruta, las
        comisiones de los pools y la de Jupiter)."""
        decimals = await self.token_decimals(mint, rpc_url)
        data = await self.quote(WSOL_MINT, mint, round(sol * _LAMPORTS_PER_SOL), priority)
        return _as_int(data["outAmount"]) / 10 ** decimals

    async def sell_price(self, mint: str, token_amount: float, rpc_url: str,
                         priority: int = PRIORITY_PRICE) -> float:
        """Precio en SOL/token al que se venderían AHORA `token_amount`
        tokens: SOL que daría Jupiter / tokens. Es el precio real de salida
        de la posición (ruta, comisiones, impacto y el impuesto de
        transferencia de Token-2022 incluidos), no un precio "de pizarra"."""
        if not token_amount or token_amount <= 0:
            raise JupiterError("No hay cantidad de tokens con la que cotizar la venta.")
        decimals = await self.token_decimals(mint, rpc_url)
        raw = int(token_amount * 10 ** decimals)
        if raw <= 0:
            raise JupiterError(f"Cantidad de tokens demasiado pequeña para cotizar ({token_amount}).")
        data = await self.quote(mint, WSOL_MINT, raw, priority)
        return _as_int(data["outAmount"]) / _LAMPORTS_PER_SOL / (raw / 10 ** decimals)

    # ---- Órdenes ---------------------------------------------------------- #

    async def wallet_token_raw_balance(self, mint: str, rpc_url: str) -> Optional[int]:
        """Unidades crudas de `mint` en la wallet (suma de sus token
        accounts). None si falla la consulta."""
        if self.keypair is None:
            return None
        try:
            async with AsyncClient(rpc_url) as client:
                resp = await client.get_token_accounts_by_owner_json_parsed(
                    self.keypair.pubkey(), TokenAccountOpts(mint=Pubkey.from_string(mint)),
                    commitment=Confirmed,
                )
            total = 0
            for acc in rpc_value(resp, "las token accounts de la wallet"):
                parsed: Any = acc.account.data.parsed
                total += _as_int(parsed["info"]["tokenAmount"].get("amount"))
            return total
        except Exception as e:
            logger.debug(f"[Jupiter] No se pudo consultar el saldo de {mint}: {e}")
            return None

    def _sign(self, tx_b64: str) -> str:
        """Firma la transacción de /order con nuestra clave, en NUESTRO hueco
        de firma: si Jupiter la enrutó por un market maker (JupiterZ), la
        firma de él ya viene puesta y no hay que pisarla."""
        assert self.keypair is not None
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        message = tx.message
        keys = list(message.account_keys)
        pubkey = self.keypair.pubkey()
        if pubkey not in keys or keys.index(pubkey) >= message.header.num_required_signatures:
            raise JupiterError("La transacción de Jupiter no pide la firma de nuestra wallet.")
        signatures = list(tx.signatures)
        signatures[keys.index(pubkey)] = self.keypair.sign_message(to_bytes_versioned(message))
        return base64.b64encode(bytes(VersionedTransaction.populate(message, signatures))).decode()

    async def execute_trade(self, action: str, mint: str, amount, slippage: float, priority_fee: float,
                            solana_rpc_url: str, tx_confirm_timeout_seconds: float = 30.0, **_ignored) -> dict:
        """Compra (`amount` en SOL) o vende TODO el saldo de `mint` de la
        wallet por Jupiter. Misma forma de respuesta y mismos errores que
        PumpPortalClient.execute_lightning_trade, para que el executor las
        trate igual:
          - RuntimeError: seguro que no se ejecutó nada.
          - TradeOutcomeUnknown: puede haber entrado; hay que mirar la wallet.
          - dict con "signature" y, si se pudo leer la tx, "actual_sol_delta"
            / "actual_token_delta" (fill real, comisiones incluidas)."""
        if self.keypair is None:
            raise RuntimeError("Para operar en real por Jupiter falta la clave privada de la wallet "
                               "(wallet_private_key en [jupiter] o PEPUMP_WALLET_PRIVATE_KEY).")
        if action == "buy":
            input_mint, output_mint = WSOL_MINT, mint
            amount_raw = round(float(amount) * _LAMPORTS_PER_SOL)
        else:
            input_mint, output_mint = mint, WSOL_MINT
            balance = await self.wallet_token_raw_balance(mint, solana_rpc_url)
            if balance is None:
                raise RuntimeError(f"No se pudo leer el saldo de {mint} en la wallet para venderlo.")
            if balance <= 0:
                # Una venta anterior que dimos por fallida pudo haber entrado:
                # que el executor lo confirme mirando la wallet.
                raise TradeOutcomeUnknown(f"La wallet ya no tiene tokens de {mint}.")
            amount_raw = balance

        params = {
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
            "taker": str(self.keypair.pubkey()),
            "slippageBps": round(slippage * 100),
            # El priority fee de la config como TOPE: Jupiter elige cuánto
            # hace falta hasta ese máximo (los reintentos de venta lo suben).
            "priorityFeeLamports": round(priority_fee * _LAMPORTS_PER_SOL),
            "broadcastFeeType": "maxCap",
        }
        order = await self._get("/order", params, PRIORITY_TRADE)
        tx_b64 = order.get("transaction")
        if not tx_b64 or not order.get("requestId"):
            motivo = order.get("errorMessage") or order.get("error") or "sin transacción"
            raise RuntimeError(f"Jupiter no pudo armar la orden de {action}: {motivo} "
                               f"(código {order.get('errorCode')}, router {order.get('router')})")
        signed = self._sign(tx_b64)

        # /execute tiene su propio cupo (no pasa por el limitador) y espera
        # a que la tx entre o caduque, así que su timeout es más largo.
        try:
            resp = await asyncio.to_thread(
                requests.post, f"{self.BASE_URL}/execute",
                json={"signedTransaction": signed, "requestId": order["requestId"]},
                headers=self._headers(),
                timeout=max(tx_confirm_timeout_seconds, self.HTTP_TIMEOUT_SECONDS) + 30,
            )
        except requests.exceptions.ConnectTimeout as e:
            raise RuntimeError(f"No se pudo conectar con Jupiter para enviar la orden: {e}") from e
        except requests.exceptions.RequestException as e:
            raise TradeOutcomeUnknown(f"Jupiter no respondió al enviar la orden de {action} ({e}); "
                                      f"no se sabe si la tx entró") from e
        if resp.status_code >= 500:
            raise TradeOutcomeUnknown(f"Jupiter devolvió {resp.status_code} al enviar la orden de "
                                      f"{action}; no se sabe si la tx entró: {resp.text[:300]}")
        if resp.status_code != 200:
            raise RuntimeError(f"Jupiter rechazó la orden de {action} ({resp.status_code}): {resp.text[:300]}")

        data = resp.json()
        signature = data.get("signature")
        if data.get("status") != "Success":
            code = data.get("code")
            enlace = f" https://solscan.io/tx/{signature}" if signature else ""
            texto = f"la orden de {action} por Jupiter falló (código {code}: {data.get('error')}){enlace}"
            if code in _EXECUTE_NOT_LANDED:
                raise RuntimeError(texto[0].upper() + texto[1:])
            raise TradeOutcomeUnknown(texto)
        if not signature:
            raise TradeOutcomeUnknown(f"Jupiter dio la orden de {action} por buena pero sin firma.")
        logger.info(f"[Jupiter] {action} confirmada: https://solscan.io/tx/{signature}")

        result: dict = {"signature": signature, "router": order.get("router"),
                        "fee_bps": order.get("feeBps")}
        fill = await fetch_actual_fill(signature, solana_rpc_url, mint, wallet=str(self.keypair.pubkey()))
        if fill is not None:
            result["actual_sol_delta"] = fill["sol_delta"]
            result["actual_token_delta"] = fill["token_delta"]
            result["actual_rent_paid_sol"] = fill.get("rent_paid_sol", 0.0)
            result["actual_rent_refund_sol"] = fill.get("rent_refund_sol", 0.0)
        else:
            logger.warning(f"[Jupiter] No se pudieron leer los datos reales de fill de {signature}; "
                           f"se usa el estimado.")
        return result


def build_jupiter_client(config) -> Optional[JupiterClient]:
    """JupiterClient a partir de la config, o None si no hay jupiter_api_key.
    Sin wallet_private_key se puede cotizar y simular, pero no operar en real.

    ValueError si la clave no es válida o no es la de
    pumpportal.wallet_address: el bot usa UNA sola wallet para las dos
    APIs, y con dos distintas los saldos y las comprobaciones de la
    wallet mirarían la equivocada."""
    private_key = config.wallet_private_key
    if not config.jupiter_api_key:
        if private_key:
            logger.warning("Hay wallet_private_key pero no jupiter_api_key: la clave no se usa.")
        if config.jupiter_for_all:
            logger.warning("jupiter_for_all = true pero no hay jupiter_api_key: todas las órdenes "
                           "siguen yendo por PumpPortal.")
        return None
    keypair = load_keypair(private_key) if private_key else None
    if keypair is not None and config.wallet_address and str(keypair.pubkey()) != config.wallet_address:
        raise ValueError(f"wallet_private_key es la clave de la wallet {keypair.pubkey()}, pero "
                         f"pumpportal.wallet_address es {config.wallet_address}. Tienen que ser la misma.")
    if keypair is None:
        logger.info("Jupiter activado solo para cotizar y simular: sin wallet_private_key no se puede "
                    "operar en real por Jupiter.")
        if config.jupiter_for_all:
            logger.warning("jupiter_for_all = true pero no hay wallet_private_key: en REAL las órdenes "
                           "siguen yendo por PumpPortal (en simulado, por Jupiter).")
    elif config.jupiter_for_all:
        logger.info("jupiter_for_all: todas las órdenes van por Jupiter.")
    return JupiterClient(config.jupiter_api_key, keypair, config.jupiter_request_interval_seconds)


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            raise ValueError("carácter no válido en base58")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _retry_after_seconds(resp) -> float:
    try:
        return max(float(resp.headers.get("Retry-After", 2)), 0.5)
    except (TypeError, ValueError):
        return 2.0
