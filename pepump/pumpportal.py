"""Cliente de PumpPortal: feed de trades (subscribeTokenTrade) y órdenes
reales por la Lightning Transaction API, con su confirmación on-chain."""
import asyncio
import json
import logging
import math
import time
from typing import Any, AsyncIterator, Optional

import requests
import websockets
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts
from solders.pubkey import Pubkey
from solders.signature import Signature

from pepump.onchain.rpc import rpc_value
from pepump.onchain.txinfo import describe_onchain_error, fetch_actual_fill

logger = logging.getLogger(__name__)


def as_positive_float(value) -> Optional[float]:
    """Convierte a float y devuelve el valor SOLO si es finito y > 0.
    None en cualquier otro caso (ausente, string no numérico, 0,
    negativo, NaN, inf). Los feeds no siempre mandan los números como
    números, y un 0 o un NaN colado en un precio es peor que no tener
    precio: dispara ventas/compras contra un valor inventado."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


class TradeOutcomeUnknown(RuntimeError):
    """La orden pudo haber salido o no: el POST a la Lightning API se
    cortó (timeout/conexión) después de mandarse, o la tx no confirmó a
    tiempo. A diferencia de un rechazo explícito, aquí NO se puede
    asumir que no pasó nada -PumpPortal puede haber firmado y enviado
    la tx igual-, así que el llamador debe mirar la wallet on-chain
    antes de decidir (ver TradeExecutor.sell)."""


class PumpPortalClient:
    """
    Encapsula toda la comunicación de red con PumpPortal. No conoce nada
    sobre estrategia de trading: solo sabe pedir datos y mandar órdenes
    a través de la Lightning Transaction API (PumpPortal firma y envía la
    transacción por su cuenta; nosotros solo necesitamos la API key).
    """

    DATA_WS_URL = "wss://pumpportal.fun/api/data"
    LIGHTNING_TRADE_URL = "https://pumpportal.fun/api/trade"

    # Supply estándar de un token creado en pump.fun (1.000.000.000 tokens),
    # usado para derivar el precio a partir de `marketCapSol` en los trades
    # que ya no traen las reservas de la bonding curve (token migrado a
    # PumpSwap). Ver extract_price.
    TOTAL_SUPPLY_TOKENS = 1_000_000_000

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        # Dirección de la wallet asociada a la api_key. La Lightning API
        # no la expone, así que se aprende del fee payer de la primera tx
        # confirmada cuyo fill se pudo leer (normalmente la compra). Hace
        # falta para get_wallet_token_balance.
        self.wallet_address: Optional[str] = None

    # ---- Feed de precios (SOLO PumpPortal, subscribeTokenTrade) ---------- #

    async def connect_trade_stream(self, mint: str):
        """
        Abre la conexión al websocket de datos de PumpPortal y hace el
        subscribe_trade (`subscribeTokenTrade`) al `mint` indicado, dejando
        la conexión abierta para que se pueda reutilizar tanto para
        conseguir el precio inicial (antes de comprar) como para el
        monitoreo posterior, sin reconectar ni volver a suscribirse.

        OJO: `subscribeTokenTrade` es un stream medido de PumpPortal (0.01
        SOL cada 10.000 eventos) y requiere una API key vinculada a una
        wallet con al menos 0.02 SOL, sin importar si el bot está en modo
        SIMULADO o REAL. Sin API key, la conexión y la suscripción NO dan
        error, pero tampoco entregan ningún trade: el bot se queda
        esperando para siempre (no hay otra fuente de precio de respaldo).
        """
        url = self.DATA_WS_URL
        if self.api_key:
            url = f"{url}?api-key={self.api_key}"
        else:
            logger.warning("Sin pumpportal.api_key configurada: subscribeTokenTrade NO va a entregar "
                           "ningún trade (requiere API key + wallet con >= 0.02 SOL, aunque el bot esté en "
                           "modo SIMULADO). Como este bot usa SOLO PumpPortal para el precio, se va a quedar "
                           "esperando para siempre.")

        ws = await websockets.connect(url)  # type: ignore[attr-defined]
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
        return ws

    @staticmethod
    async def iter_trade_events(ws) -> AsyncIterator[dict]:
        """Va entregando cada evento crudo (dict) que llega por una conexión
        ya abierta y suscripta (ver connect_trade_stream)."""
        async for raw_msg in ws:
            try:
                event = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue
            # BUGFIX: un mensaje JSON válido pero que NO es un objeto
            # (una lista, un string suelto) llegaba de todos modos hasta
            # extract_price()/`event.keys()` y reventaba con
            # AttributeError. Esa excepción no la atrapa el camino de
            # entrada (_get_reference_price la deja subir), así que
            # tumbaba el bot con un traceback crudo. Aquí se descarta.
            if not isinstance(event, dict):
                logger.debug(f"[Feed de trades] mensaje descartado (no es un objeto JSON): {event!r}")
                continue
            yield event

    @classmethod
    def extract_price(cls, event: dict) -> Optional[float]:
        """
        Precio en SOL/token a partir de un evento de subscribeTokenTrade.
        Tres niveles, todos con datos que PumpPortal ya manda en el propio
        evento — sin recurrir a ninguna fuente externa (DexScreener, etc.)
        que podría desalinearse del precio real de ejecución y meter
        slippage entre lo que el bot "ve" y lo que realmente paga:

        1. Mientras el token sigue en la bonding curve de pump.fun, el
           evento trae las reservas virtuales (`vSolInBondingCurve`/
           `vTokensInBondingCurve`) y el precio sale de ahí, exacto.

        2. Si el token ya migró a PumpSwap/Raydium, esas reservas dejan de
           venir; en ese caso se usa `marketCapSol` -que sí viene en todos
           los trades, migrados o no- junto con el supply estándar de
           pump.fun (1.000.000.000 tokens) para derivar el precio.

        3. Si tampoco viene `marketCapSol` (algunos trades de pools ya
           migrados no lo incluyen), se cae al precio efectivo de ESE
           trade puntual: `solAmount / tokenAmount`, los importes reales que
           se intercambiaron en esa operación. Es el nivel menos preciso
           de los tres (es el precio de UN trade, no una cotización
           instantánea de reservas), pero sigue siendo 100% PumpPortal,
           en vivo, sin fuentes externas.

        BUGFIX: el nivel 1 no tenía ninguna de las guardas que sí tenían
        el 2 y el 3. Un evento con esos campos como string reventaba con
        TypeError, y esa excepción subía hasta el `except Exception` de
        _consume_trade_stream, que la logueaba como "conexión
        interrumpida" y disparaba una reconexión con backoff que no
        hacía falta. Y un `vSolInBondingCurve` en 0 devolvía 0.0, que el
        llamador descarta como "sin precio" -en vez de seguir al nivel 2
        (marketCapSol), que probablemente sí tenía el dato. Ahora los
        tres niveles convierten y validan del mismo modo, y un nivel que
        no da un precio positivo cae al siguiente en lugar de cortar la
        cadena.
        """
        v_sol = as_positive_float(event.get("vSolInBondingCurve"))
        v_tok = as_positive_float(event.get("vTokensInBondingCurve"))
        if v_sol is not None and v_tok is not None:
            price = v_sol / v_tok
            if price > 0:
                return price

        market_cap_sol = as_positive_float(event.get("marketCapSol"))
        if market_cap_sol is not None:
            price = market_cap_sol / cls.TOTAL_SUPPLY_TOKENS
            if price > 0:
                return price

        sol_amount = as_positive_float(event.get("solAmount"))
        token_amount = as_positive_float(event.get("tokenAmount"))
        if sol_amount is not None and token_amount is not None:
            price = sol_amount / token_amount
            if price > 0:
                return price

        return None

    # ---- Trading real (Lightning Transaction API) ------------------------- #

    async def execute_lightning_trade(self, action: str, mint: str, amount, denominated_in_sol: bool,
                                       slippage: float, priority_fee: float, pool: str,
                                       solana_rpc_url: str,
                                       tx_confirm_timeout_seconds: float = 30.0,
                                       tx_confirm_poll_interval_seconds: float = 2.0) -> dict:
        """
        Manda la orden a la Lightning API y, si consigue una firma, se
        queda esperando la confirmación REAL on-chain antes de dar la
        operación por buena. Esto es necesario porque PumpPortal puede
        devolver una firma con 200 OK de forma "optimista" -antes de que
        la transacción se confirme en la red- y esa transacción puede
        reventar después on-chain (típicamente por slippage excedido si
        el precio se movió entre que se armó la tx y se incluyó en un
        bloque). Sin esta comprobación, el bot trataría un 200 OK con firma
        como compra/venta exitosa aunque en la práctica no haya pasado
        nada en la wallet real.

        Lanza RuntimeError en cualquiera de estos casos (todos indican
        que NO hay que dar la operación por hecha):
          - HTTP distinto de 200.
          - 200 OK pero sin firma en el body (PumpPortal rechazó la
            orden de inmediato: slippage inválido, fondos insuficientes, etc.).
          - Firma válida pero la transacción FALLÓ on-chain (revert) —
            aquí es donde cae el caso real de "slippage excedido" que
            pasa DESPUÉS de que PumpPortal ya contestó 200.
          - Firma válida pero no confirma dentro de
            `tx_confirm_timeout_seconds` (puede seguir pendiente, pero
            no lo sabemos con certeza -> mejor tratarlo como fallo y que
            se revise a mano).
        """
        if not self.api_key:
            raise RuntimeError("Se necesita una API key de PumpPortal para operar con la Lightning API.")

        payload = {
            "action": action,  # "buy" o "sell"
            "mint": mint,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "amount": amount,
            "slippage": slippage,
            "priorityFee": priority_fee,
            "pool": pool,
        }

        # requests.post es bloqueante; lo mandamos a un thread aparte para
        # no congelar el loop de asyncio (que sigue necesitando procesar
        # el feed de precios y demás mientras se manda la orden).
        try:
            resp = await asyncio.to_thread(
                requests.post,
                f"{self.LIGHTNING_TRADE_URL}?api-key={self.api_key}",
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=15,
            )
        except requests.exceptions.ConnectTimeout as e:
            # Ni siquiera se conectó: el pedido nunca llegó a PumpPortal.
            raise RuntimeError(f"No se pudo conectar con la Lightning API: {e}") from e
        except requests.exceptions.RequestException as e:
            # Timeout de lectura / conexión cortada con el pedido ya
            # enviado: PumpPortal puede haber mandado la tx igual.
            raise TradeOutcomeUnknown(
                f"La Lightning API no respondió a la orden de {action} ({e}); "
                f"no se sabe si la tx se envió"
            ) from e
        if resp.status_code != 200:
            raise RuntimeError(f"Lightning API devolvió {resp.status_code}: {resp.text}")

        data = resp.json()
        signature = data.get("signature") if isinstance(data, dict) else None
        if not signature:
            raise RuntimeError(f"PumpPortal rechazó la orden de {action}: {data}")

        await self._confirm_transaction_onchain(
            signature, solana_rpc_url, tx_confirm_timeout_seconds, tx_confirm_poll_interval_seconds
        )

        # La tx YA confirmó on-chain (lo de arriba lanza una excepción si
        # no es así). Ahora
        # leemos los datos REALES de fill (SOL y tokens que realmente se
        # movieron en la wallet) para no depender del precio de
        # referencia estimado -ver executor.py, que usa esto para armar
        # el Position con números reales en vez de estimados. Si por lo
        # que sea esto falla (RPC caído, formato inesperado, etc.), no
        # hacemos fallar la operación -ya sabemos que confirmó bien-,
        # simplemente no viene el fill real y el llamador cae al
        # estimado como respaldo.
        fill = await fetch_actual_fill(signature, solana_rpc_url, mint)
        if fill is not None:
            self.wallet_address = fill["wallet"]
            data["actual_sol_delta"] = fill["sol_delta"]
            data["actual_token_delta"] = fill["token_delta"]
            data["actual_rent_paid_sol"] = fill.get("rent_paid_sol", 0.0)
            data["actual_rent_refund_sol"] = fill.get("rent_refund_sol", 0.0)
            data["actual_created_mints"] = fill.get("created_mints", [])
        else:
            logger.warning(f"[REAL] No se pudieron leer los datos reales de fill de la tx "
                            f"{signature}; se van a usar los valores estimados para esta operación.")
        return data

    async def get_sol_balance(self, rpc_url: str, address: Optional[str] = None) -> Optional[float]:
        """SOL de la wallet (`address`, o la de la api_key si ya se conoce).
        None si no se conoce la wallet o falla la consulta."""
        address = address or self.wallet_address
        if not address:
            return None
        try:
            async with AsyncClient(rpc_url) as client:
                resp = await client.get_balance(Pubkey.from_string(address), commitment=Confirmed)
            return rpc_value(resp, "el saldo de la wallet") / 1e9
        except Exception as e:
            logger.debug(f"No se pudo consultar el saldo de SOL de la wallet: {e}")
            return None

    async def get_wallet_token_balance(self, mint: str, rpc_url: str) -> Optional[float]:
        """Tokens de `mint` que tiene ahora mismo la wallet (suma de todas
        sus token accounts de ese mint; 0.0 si no tiene ninguna). None si
        todavía no se conoce la wallet o si falla la consulta."""
        if not self.wallet_address:
            return None
        try:
            async with AsyncClient(rpc_url) as client:
                resp = await client.get_token_accounts_by_owner_json_parsed(
                    Pubkey.from_string(self.wallet_address),
                    TokenAccountOpts(mint=Pubkey.from_string(mint)),
                    commitment=Confirmed,
                )
            total = 0.0
            for acc in resp.value:
                parsed: Any = acc.account.data.parsed  # JSON de jsonParsed, sin tipos útiles
                ui_amount = parsed["info"]["tokenAmount"].get("uiAmount")
                total += float(ui_amount) if ui_amount is not None else 0.0
            return total
        except Exception as e:
            logger.debug(f"No se pudo consultar el saldo de {mint} en la wallet: {e}")
            return None

    @staticmethod
    async def _confirm_transaction_onchain(signature: str, rpc_url: str,
                                            timeout_seconds: float, poll_interval_seconds: float) -> None:
        """Poll a Solana RPC hasta que la tx confirme (o falle, o venza
        el timeout). No devuelve nada si confirmó bien; lanza
        RuntimeError en cualquier otro caso."""
        sig = Signature.from_string(signature)
        deadline = time.monotonic() + timeout_seconds
        async with AsyncClient(rpc_url) as client:
            while True:
                resp = await client.get_signature_statuses([sig], search_transaction_history=True)
                value = resp.value
                info = value[0] if value else None
                if info is not None:
                    if info.err is not None:
                        short_reason, debug_detail = await describe_onchain_error(signature, rpc_url, info.err)
                        logger.debug(f"[Confirmación on-chain] detalle técnico de la falla de "
                                     f"{signature}:\n  {debug_detail}")
                        raise RuntimeError(
                            f"La transacción {signature} FALLÓ on-chain{short_reason}. "
                            f"https://solscan.io/tx/{signature}"
                        )
                    if info.confirmation_status is not None:
                        return  # processed/confirmed/finalized: ya sabemos que NO falló

                if time.monotonic() >= deadline:
                    raise TradeOutcomeUnknown(
                        f"La transacción {signature} no confirmó en {timeout_seconds:.1f}s "
                        f"(puede seguir pendiente) — revisa https://solscan.io/tx/{signature}"
                    )
                await asyncio.sleep(poll_interval_seconds)
