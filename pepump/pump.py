import requests
import websockets
import json
import logging
import asyncio
import math
import time
from typing import AsyncIterator, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey  # type: ignore
from solders.signature import Signature  # type: ignore
from pumpswapamm.pumpswapamm import fetch_pool_state
from pumpswapamm.fetch_reserves import fetch_pool_base_price

from pepump.onchain_errors import describe_custom_error, failing_program_from_logs

logger = logging.getLogger(__name__)

# Programa de PumpSwap en Solana (constante pública, no cambia).
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Programa de pump.fun (bonding curve) en Solana (constante pública, no cambia).
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Mint del SOL "wrapped" (WSOL) — para confirmar que el pool que
# encontramos está denominado en SOL antes de usar su precio.
WSOL_MINT = "So11111111111111111111111111111111111111112"

def _as_positive_float(value) -> Optional[float]:
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


def _extract_instruction_error(err) -> Optional[tuple]:
    """Si `err` (el `.err` de getSignatureStatuses) es un
    TransactionErrorInstructionError con un código Custom(N), devuelve
    (índice_de_instrucción, código). None si no matchea esa forma (otro
    tipo de error, versión distinta de solders, etc.) -nunca tira
    excepción, esto es solo para dar un mensaje más claro, no crítico."""
    try:
        from solders.transaction_status import (
            TransactionErrorInstructionError,
            InstructionErrorCustom,
        )
        if isinstance(err, TransactionErrorInstructionError):
            inner = err.err
            if isinstance(inner, InstructionErrorCustom):
                return err.index, inner.code
    except Exception:
        pass
    return None


def _relevant_log_lines(logs: list[str]) -> list[str]:
    """Filtra los logMessages a las líneas que parecen explicar la falla."""
    keywords = ("error", "Error", "fail", "Fail", "insufficient", "Insufficient",
                "slippage", "Slippage", "revert", "exceed", "Exceed")
    relevant = [line for line in logs if any(k in line for k in keywords)]
    return relevant[-5:] if relevant else []


async def _fetch_program_logs(signature: str, rpc_url: str) -> list[str]:
    """Pide la transacción completa (getTransaction) y devuelve TODOS sus
    logMessages. Se necesitan completos -no solo las líneas que mencionan
    un error- porque la línea "Program <id> invoke/failed" es la que
    permite saber QUÉ programa revirtió, y de eso depende contra qué
    tabla de errores se traduce el código Custom(N) (ver
    onchain_errors.py: el mismo número significa cosas distintas en
    pump.fun y en PumpSwap). El filtrado a lo "relevante" para mostrar se
    hace después, en _relevant_log_lines.

    Devuelve lista vacía si no se puede obtener nada (no debe hacer
    fallar la confirmación por esto, es solo información extra)."""
    try:
        sig = Signature.from_string(signature)
        async with AsyncClient(rpc_url) as client:
            resp = await client.get_transaction(
                sig, encoding="json", max_supported_transaction_version=0
            )
        if resp.value is None or resp.value.transaction is None:
            return []
        meta = resp.value.transaction.meta
        logs = meta.log_messages if meta is not None else None
        return list(logs) if logs else []
    except Exception as e:
        logger.debug(f"[Confirmación on-chain] no se pudieron leer los logs de {signature} "
                     f"para un mensaje de error más claro: {e}")
        return []


async def _describe_onchain_error(signature: str, rpc_url: str, err) -> tuple[str, str]:
    """Devuelve (razón_corta, detalle_técnico):
      - razón_corta: UNA frase legible para meter directo en el mensaje
        de error principal (ej. ": pump.fun BondingCurveComplete (código
        6005, instrucción #3)"). Vacía si no se pudo determinar nada
        mejor que el error crudo.
      - detalle_técnico: el error crudo de solders + los logs relevantes
        de la transacción, para loguear aparte a nivel DEBUG.

    BUGFIX: antes, cualquier código Custom(N) se buscaba SIEMPRE en la
    tabla de spl-token. Eso hacía dos cosas mal a la vez: los errores
    propios de pump.fun/PumpSwap (Anchor, 6000 para arriba) nunca
    matcheaban y caían en el genérico "revirtió con un error no
    reconocido" -incluido el 6005 BondingCurveComplete, justo el que más
    aparece cuando el ruteo del pool quedó desalineado con la migración-,
    y un código bajo se atribuía a spl-token aunque lo hubiese tirado
    otro programa. Ahora se identifica primero QUÉ programa revirtió
    (línea "Program <id> failed" de los logs) y recién ahí se traduce el
    código contra la tabla de ESE programa."""
    decoded = _extract_instruction_error(err)
    logs = await _fetch_program_logs(signature, rpc_url)
    relevant = _relevant_log_lines(logs)

    debug_lines = [f"error crudo: {err}"]
    program_id = failing_program_from_logs(logs)
    if program_id:
        debug_lines.append(f"programa que revirtió: {program_id}")
    if relevant:
        debug_lines.append("logs relevantes del programa:\n  " + "\n  ".join(relevant))
    detail = "\n  ".join(debug_lines)

    if decoded is not None:
        ix_index, code = decoded
        described = describe_custom_error(code, program_id)
        if described:
            return f": {described} (código {code}, instrucción #{ix_index})", detail

    if relevant:
        # Sin código traducible, pero SÍ hay algún log relevante
        # (típicamente el motivo real que imprime el propio programa que
        # revirtió, ej. un AnchorError con "Error Message: ...").
        return f": {relevant[-1].strip()}", detail

    return ": revirtió con un error no reconocido (corré con -v para ver más detalle, o abrí el link de Solscan)", detail


def _wallet_token_delta(meta, mint: str, wallet: str) -> Optional[float]:
    """Cuántos tokens de `mint` ganó (positivo) o gastó (negativo) LA
    WALLET en esta transacción, a partir de los token balances pre/post
    de la meta. None si no se puede identificar con certeza cuál de las
    token accounts que aparecen es la nuestra.

    BUGFIX (la compra SIEMPRE caía al estimado): la versión anterior
    elegía, para cada lado, "la entrada del mint que sea de la wallet
    y, si ninguna lo es pero hay UNA sola entrada de ese mint, esa".
    Ese último atajo es justo el que rompía todas las compras:

      - En una COMPRA de un mint nuevo, `pre_token_balances` NO trae
        ninguna cuenta nuestra (todavía no existe nuestra ATA), pero SÍ
        trae la token account de la bonding curve de pump.fun -que es
        la que tiene los tokens y la que la tx toca-. Como era la única
        entrada de ese mint, se la tomaba como "nuestra": pre_tokens
        quedaba en cientos de millones de tokens (los de la curva).
        `post_token_balances` sí traía nuestra ATA, así que post_tokens
        eran los tokens comprados -> token_delta daba un número
        enormemente NEGATIVO, y executor.buy exige `> 0` para aceptar
        el fill real. De ahí que la compra terminara SIEMPRE en el
        estimado aunque la tx confirmara perfecto.

      - En una VENTA no pasaba: nuestra ATA ya existe y aparece con
        `owner` en los dos lados, así que el atajo nunca se usaba. Por
        eso la venta sí mostraba datos reales (el síntoma exacto
        reportado).

    Ahora la wallet se identifica SOLO por `owner`, y "nuestra cuenta
    no aparece de este lado" se interpreta como 0 tokens (que es
    literalmente lo que había: la cuenta no existía todavía), no como
    "usá la cuenta de otro". El atajo de la única entrada queda
    reservado para el caso en que el nodo RPC no mande `owner` en
    NINGUNA entrada, y solo si de verdad hay una sola token account de
    ese mint en toda la tx.
    """
    pre = [b for b in (meta.pre_token_balances or []) if str(b.mint) == mint]
    post = [b for b in (meta.post_token_balances or []) if str(b.mint) == mint]
    todos = pre + post
    if not todos:
        return None

    if any(getattr(b, "owner", None) is not None for b in todos):
        def es_nuestra(b) -> bool:
            owner = getattr(b, "owner", None)
            return owner is not None and str(owner) == wallet
    else:
        # Ningún balance trae `owner` (algunos nodos lo omiten). Solo
        # podemos asumir que la cuenta es nuestra si hay UNA sola token
        # account de ese mint en toda la transacción; si hay varias, no
        # adivinamos y el llamador cae al estimado.
        indices = {getattr(b, "account_index", None) for b in todos}
        if None in indices:
            if len(pre) > 1 or len(post) > 1:
                return None
        elif len(indices) > 1:
            return None

        def es_nuestra(b) -> bool:
            return True

    def _total_propio(balances) -> Optional[float]:
        propias = [b for b in balances if es_nuestra(b)]
        if not propias:
            return None
        total = 0.0
        for b in propias:
            ui_amount = b.ui_token_amount.ui_amount
            total += float(ui_amount) if ui_amount is not None else 0.0
        return total

    pre_tokens = _total_propio(pre)
    post_tokens = _total_propio(post)
    # Si nuestra cuenta no aparece en NINGUNO de los dos lados, no
    # pudimos identificar el movimiento de tokens -no asumimos 0.
    if pre_tokens is None and post_tokens is None:
        return None
    return (post_tokens or 0.0) - (pre_tokens or 0.0)


async def _fetch_actual_fill(signature: str, rpc_url: str, mint: str,
                              max_attempts: int = 3, retry_delay_seconds: float = 0.75) -> Optional[dict]:
    """Lee la transacción YA CONFIRMADA (ver _confirm_transaction_onchain,
    que se llama SIEMPRE antes que esto) y calcula, a partir de los
    balances reales pre/post en la wallet, cuánto SOL neto se movió y
    cuántos tokens del `mint` se movieron REALMENTE -no una estimación
    del precio de referencia de antes de mandar la orden.

    La wallet se identifica como el PRIMER account_key de la
    transacción: en Solana, el fee payer/firmante principal siempre va
    en el índice 0 del mensaje, y las órdenes de la Lightning API de
    PumpPortal las firma y paga siempre la wallet asociada a la
    api_key -así que ese índice 0 es, siempre, nuestra propia wallet.

    Devuelve None si por lo que sea no se puede leer/parsear la tx (el
    llamador debe caer entonces al precio/monto ESTIMADO en vez de
    fallar la operación por esto: la compra/venta YA CONFIRMÓ on-chain
    -eso ya lo garantizó _confirm_transaction_onchain-, esto es solo
    para reportar números reales, no para decidir si salió bien).

    Devuelve {"sol_delta": float, "token_delta": float}:
      - sol_delta: SOL netos ganados (positivo) o gastados (negativo)
        por la wallet en esta tx, en SOL (no lamports) e incluyendo
        TODOS los fees (red + priority fee + lo que haya cobrado el
        programa) -es el movimiento real de saldo, no un cálculo.
      - token_delta: tokens del `mint` ganados (positivo) o gastados
        (negativo) por la wallet, en unidades de token (no raw/atomic).

    `max_attempts`/`retry_delay_seconds`: get_transaction puede no
    encontrar todavía la tx (resp.value/transaction en None) aunque
    _confirm_transaction_onchain ya la haya dado por confirmada -hay un
    desfasaje real entre "el status ya dice confirmed/finalized" y "ya
    está indexada y consultable vía getTransaction" en el nodo RPC, sea
    el mismo nodo u otro. Reintentamos un par de veces con una espera
    corta antes de rendirnos y caer al estimado. Los demás casos de
    "no se puede leer" (falta meta, faltan balances, el mint no aparece
    en ningún lado) son estructurales -no un tema de timing- así que no
    tiene sentido reintentarlos, pero de todos modos no cuesta nada
    dejar que también consuman intentos si por lo que sea cambian entre
    llamadas.
    """
    try:
        sig = Signature.from_string(signature)
    except Exception as e:
        logger.warning(f"No se pudieron leer los datos reales de fill de {signature} "
                        f"(firma inválida, se va a usar el estimado como respaldo): {e}")
        return None

    for attempt in range(1, max_attempts + 1):
        try:
            async with AsyncClient(rpc_url) as client:
                resp = await client.get_transaction(
                    sig, encoding="json", commitment=Confirmed,
                    max_supported_transaction_version=0,
                )
            if resp.value is None or resp.value.transaction is None:
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay_seconds)
                    continue
                return None

            meta = resp.value.transaction.meta
            if meta is None or not meta.pre_balances or not meta.post_balances:
                return None

            account_keys = resp.value.transaction.transaction.message.account_keys
            if not account_keys:
                return None
            wallet = str(account_keys[0])

            sol_delta = (meta.post_balances[0] - meta.pre_balances[0]) / 1_000_000_000.0

            # Movimiento real de tokens de NUESTRA wallet -ver
            # _wallet_token_delta, que es donde vive la identificación de
            # cuál de las token accounts de la tx es la nuestra (y el
            # bugfix por el que las compras caían siempre al estimado).
            token_delta = _wallet_token_delta(meta, mint, wallet)
            if token_delta is None:
                return None

            return {"sol_delta": sol_delta, "token_delta": token_delta}
        except Exception as e:
            if attempt < max_attempts:
                await asyncio.sleep(retry_delay_seconds)
                continue
            logger.warning(f"No se pudieron leer los datos reales de fill de {signature} "
                            f"(se va a usar el estimado como respaldo, tras {max_attempts} intentos): {e}")
            return None
    return None  # inalcanzable (el loop siempre retorna o sigue), queda por claridad


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

        ws = await websockets.connect(url)
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
            # (una lista, un string suelto) llegaba igual hasta
            # extract_price()/`event.keys()` y reventaba con
            # AttributeError. Esa excepción no la atrapa el camino de
            # entrada (_get_reference_price la deja subir), así que
            # tumbaba el bot con un traceback crudo. Acá se descarta.
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
           trade puntual: `solAmount / tokenAmount`, los montos reales que
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
        tres niveles convierten y validan igual, y un nivel que no da un
        precio positivo cae al siguiente en lugar de cortar la cadena.
        """
        v_sol = _as_positive_float(event.get("vSolInBondingCurve"))
        v_tok = _as_positive_float(event.get("vTokensInBondingCurve"))
        if v_sol is not None and v_tok is not None:
            price = v_sol / v_tok
            if price > 0:
                return price

        market_cap_sol = _as_positive_float(event.get("marketCapSol"))
        if market_cap_sol is not None:
            price = market_cap_sol / cls.TOTAL_SUPPLY_TOKENS
            if price > 0:
                return price

        sol_amount = _as_positive_float(event.get("solAmount"))
        token_amount = _as_positive_float(event.get("tokenAmount"))
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
        bloque). Sin este chequeo, el bot trataría un 200 OK con firma
        como compra/venta exitosa aunque en la práctica no haya pasado
        nada en la wallet real.

        Lanza RuntimeError en cualquiera de estos casos (todos indican
        que NO hay que dar la operación por hecha):
          - HTTP distinto de 200.
          - 200 OK pero sin firma en el body (PumpPortal rechazó la
            orden de una: slippage inválido, fondos insuficientes, etc.).
          - Firma válida pero la transacción FALLÓ on-chain (revert) —
            acá es donde cae el caso real de "slippage excedido" que
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
        resp = await asyncio.to_thread(
            requests.post,
            f"{self.LIGHTNING_TRADE_URL}?api-key={self.api_key}",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Lightning API devolvió {resp.status_code}: {resp.text}")

        data = resp.json()
        signature = data.get("signature") if isinstance(data, dict) else None
        if not signature:
            raise RuntimeError(f"PumpPortal rechazó la orden de {action}: {data}")

        await self._confirm_transaction_onchain(
            signature, solana_rpc_url, tx_confirm_timeout_seconds, tx_confirm_poll_interval_seconds
        )

        # La tx YA confirmó on-chain (lo de arriba no tira si no). Ahora
        # leemos los datos REALES de fill (SOL y tokens que realmente se
        # movieron en la wallet) para no depender del precio de
        # referencia estimado -ver executor.py, que usa esto para armar
        # el Position con números reales en vez de estimados. Si por lo
        # que sea esto falla (RPC caído, formato inesperado, etc.), no
        # hacemos fallar la operación -ya sabemos que confirmó bien-,
        # simplemente no viene el fill real y el llamador cae al
        # estimado como respaldo.
        fill = await _fetch_actual_fill(signature, solana_rpc_url, mint)
        if fill is not None:
            data["actual_sol_delta"] = fill["sol_delta"]
            data["actual_token_delta"] = fill["token_delta"]
        else:
            logger.warning(f"[REAL] No se pudieron leer los datos reales de fill de la tx "
                            f"{signature}; se van a usar los valores estimados para esta operación.")
        return data

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
                        short_reason, debug_detail = await _describe_onchain_error(signature, rpc_url, info.err)
                        logger.debug(f"[Confirmación on-chain] detalle técnico de la falla de "
                                     f"{signature}:\n  {debug_detail}")
                        raise RuntimeError(
                            f"La transacción {signature} FALLÓ on-chain{short_reason}. "
                            f"https://solscan.io/tx/{signature}"
                        )
                    if info.confirmation_status is not None:
                        return  # processed/confirmed/finalized: ya sabemos que NO falló

                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"La transacción {signature} no confirmó en {timeout_seconds:.1f}s "
                        f"(puede seguir pendiente) — revisá https://solscan.io/tx/{signature}"
                    )
                await asyncio.sleep(poll_interval_seconds)


class PumpCurveOnChainClient:
    """
    Fallback de precio para mints que TODAVÍA están en la bonding curve
    de pump.fun (no migraron a PumpSwap) -para el caso en que
    subscribeTokenTrade da el ack de suscripción pero no entrega NINGÚN
    trade real (ver bot.py: mismo síntoma que dispara
    PumpSwapOnChainClient, pero acá ya confirmado por
    `PumpSwapOnChainClient.fetch_price_or_confirm_absent` que el mint
    sigue en bonding curve -pool_confirmed_absent=True-, así que no
    tiene sentido seguir esperando a ciegas un feed que puede no estar
    entregando nada por otro motivo).

    Lee la cuenta de la bonding curve DIRECTO de Solana vía RPC -la
    misma cuenta contra la que se ejecutaría el trade real, y la misma
    fuente que usa PumpPortal para calcular vSolInBondingCurve /
    vTokensInBondingCurve- así que no mete ningún desfasaje de una
    fuente externa.

    Layout de la cuenta (estable desde el lanzamiento del programa; el
    equipo de pump.fun documentó públicamente que la cuenta CRECIÓ para
    sumar campos nuevos -ej. cashback- pero los offsets de los campos
    viejos, incluidos los que usamos acá, no cambiaron -ver
    pump-public-docs/PUMP_PROGRAM_README.md, que recomienda no depender
    del tamaño de la cuenta sino del discriminador):

      offset 0  (8 bytes): discriminador Anchor
      offset 8  (8 bytes): virtualTokenReserves (u64, little-endian, 6 decimales)
      offset 16 (8 bytes): virtualSolReserves (u64, little-endian, lamports)
      offset 24 (8 bytes): realTokenReserves (u64, little-endian)
      offset 32 (8 bytes): realSolReserves (u64, little-endian)
      offset 40 (8 bytes): tokenTotalSupply (u64, little-endian)
      offset 48 (1 byte):  complete (bool) — True = la curva ya se
                            completó (migró o está migrando a PumpSwap);
                            a partir de ahí este fallback deja de ser
                            confiable y hay que pasar a
                            PumpSwapOnChainClient.

    OJO: esto es SOLO lectura de una cuenta pública -no arma ni firma
    ninguna transacción de compra/venta (eso lo sigue haciendo
    PumpPortal Lightning API)-, así que los cambios de layout de
    INSTRUCCIONES de trading que hubo en el programa de pump.fun
    durante 2026 (la cuenta bonding-curve-v2 agregada como cuenta extra
    en las instrucciones buy/sell) no afectan nada acá: seguimos
    leyendo la cuenta bonding-curve original (v1), que es la que trae
    las reservas y no cambió de offsets.
    """

    PROGRAM_ID = PUMPFUN_PROGRAM_ID
    # pump.fun: SOL tiene 9 decimales (lamports), los tokens de pump.fun
    # siempre tienen 6 decimales (ver TOTAL_SUPPLY_TOKENS en
    # PumpPortalClient) -hace falta este ajuste porque virtualSolReserves
    # y virtualTokenReserves NO están en la misma escala entre sí (a
    # diferencia de vSolInBondingCurve/vTokensInBondingCurve, que
    # PumpPortal ya entrega convertidos a unidades humanas).
    _SOL_DECIMALS = 9
    _TOKEN_DECIMALS = 6
    _VIRTUAL_TOKEN_RESERVES_OFFSET = 8
    _VIRTUAL_SOL_RESERVES_OFFSET = 16
    _COMPLETE_OFFSET = 48
    _MIN_ACCOUNT_LEN = _COMPLETE_OFFSET + 1

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def _bonding_curve_address(self, mint: str) -> Pubkey:
        pda, _bump = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(self.PROGRAM_ID),
        )
        return pda

    async def fetch_price_or_status(self, mint: str) -> tuple[Optional[float], bool, Optional[bool]]:
        """Devuelve (price, complete, exists):
          - exists=False: CONFIRMADO que no existe cuenta de bonding
            curve para este mint (la consulta a la cuenta respondió
            bien y vino vacía) -señal fuerte de que este mint nunca se
            lanzó en pump.fun (ej. un mint nativo de Raydium/Meteora/
            otro DEX), a diferencia de exists=None (ver abajo). price y
            complete no significan nada en este caso.
          - exists=None: la consulta on-chain en sí falló (red/RPC/
            parseo) -NO es una confirmación de nada sobre la cuenta,
            a diferencia de exists=False. El llamador no debería sacar
            ninguna conclusión sobre si el mint es o no de pump.fun a
            partir de este caso.
          - exists=True, complete=True: la curva ya completó. price
            puede venir igual (últimas reservas antes de completar) pero
            el llamador NO debería seguir operando con este fallback -
            hay que pasar a PumpSwapOnChainClient.
          - exists=True, complete=False, price no-None: caso normal,
            precio válido calculado de las reservas virtuales.

        Nunca tira excepción hacia arriba (mismo criterio que
        PumpSwapOnChainClient.fetch_price_or_confirm_absent): cualquier
        error de red/RPC/parseo devuelve (None, False, None) -exists=None,
        no False- para no confundir un fallo de RPC con una confirmación
        real de que la cuenta no existe."""
        try:
            pda = self._bonding_curve_address(mint)
            async with AsyncClient(self.rpc_url) as client:
                resp = await client.get_account_info(pda, encoding="base64")
                info = resp.value
                if info is None:
                    logger.debug(f"[On-chain bonding curve] No existe cuenta de bonding curve para "
                                 f"{mint} ({pda}).")
                    return None, False, False

                data = info.data
                if len(data) < self._MIN_ACCOUNT_LEN:
                    logger.warning(f"[On-chain bonding curve] Cuenta de {mint} más chica de lo "
                                    f"esperado ({len(data)} bytes, se esperaban al menos "
                                    f"{self._MIN_ACCOUNT_LEN}); no se puede leer con este layout.")
                    return None, False, True

                v_tok = int.from_bytes(
                    data[self._VIRTUAL_TOKEN_RESERVES_OFFSET:self._VIRTUAL_TOKEN_RESERVES_OFFSET + 8],
                    "little",
                )
                v_sol = int.from_bytes(
                    data[self._VIRTUAL_SOL_RESERVES_OFFSET:self._VIRTUAL_SOL_RESERVES_OFFSET + 8],
                    "little",
                )
                complete = data[self._COMPLETE_OFFSET] != 0

                if v_tok <= 0:
                    logger.debug(f"[On-chain bonding curve] virtualTokenReserves=0 para {mint}; no se "
                                 f"puede calcular precio.")
                    return None, complete, True

                price = (v_sol / (10 ** self._SOL_DECIMALS)) / (v_tok / (10 ** self._TOKEN_DECIMALS))
                if price <= 0:
                    return None, complete, True

                logger.debug(f"[On-chain bonding curve] {pda} | reservas virtuales: "
                             f"{v_tok} tokens (raw) / {v_sol} lamports SOL | complete={complete}")
                return float(price), complete, True
        except Exception as e:
            logger.warning(f"[On-chain bonding curve] Falló la consulta on-chain para {mint}: {e}")
            return None, False, None


class PumpSwapOnChainClient:
    """
    Fallback de precio ÚNICAMENTE para mints que ya migraron a PumpSwap
    (ver bot.py: se usa solo si subscribeTokenTrade confirma el ack pero
    no entrega NINGÚN trade dentro de `live_feed_timeout_seconds` — el
    síntoma real, confirmado a mano, de un mint que ya salió de la
    bonding curve). Lee las reservas del pool DIRECTO de Solana vía RPC
    -la misma cuenta contra la que se ejecutaría el trade real- así que
    no mete ningún desfasaje de una fuente externa tipo DexScreener.

    Usa la librería `pumpswapamm` (github.com/FLOCK4H/PumpSwapAMM) solo
    para parsear la cuenta del pool; el descubrimiento del pool a partir
    del mint lo hacemos nosotros con un getProgramAccounts + memcmp
    directo sobre el programa de PumpSwap, porque esa librería no trae
    una función para "encontrar el pool de este mint" (solo puede leer
    un pool si ya conocés su dirección, o derivarla si ya conocés el
    `creator`, que para un mint migrado automáticamente desde pump.fun
    no es el wallet que creó el token).

    OJO: pumpswapamm es de un solo mantenedor y no está auditada. Se usa
    acá solo para DECODIFICAR una cuenta pública de solo lectura (no
    firma ni manda transacciones), pero aun así es una dependencia
    externa nueva — tenelo en cuenta.
    """

    # Offsets en bytes dentro de la cuenta del pool, verificados contra el
    # struct real de pumpswapamm (PumpSwapPoolStateNew/Old en
    # pumpswapamm.py). Los campos son todos de largo fijo y los dos
    # layouts (NEW/OLD) coinciden hasta `lp_supply` -solo difieren en el
    # `coin_creator` del final-, así que estos offsets valen para ambos:
    #   0   discriminador Anchor        (8)
    #   8   pool_bump                   (1)
    #   9   index                       (2)
    #   11  creator                     (32)
    #   43  base_mint                   (32)
    #   75  quote_mint                  (32)
    #   107 lp_mint                     (32)
    #   139 pool_base_token_account     (32)
    #   171 pool_quote_token_account    (32)
    #   203 lp_supply                   (u64 little-endian)
    _BASE_MINT_OFFSET = 43
    _QUOTE_MINT_OFFSET = 75
    _LP_SUPPLY_OFFSET = 203
    _MIN_POOL_ACCOUNT_LEN = _LP_SUPPLY_OFFSET + 8

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def fetch_price_for_migrated_mint(self, mint: str) -> Optional[float]:
        """Busca el pool de PumpSwap para `mint` y devuelve su precio
        actual en SOL/token leyendo las reservas on-chain. None si no
        encuentra el pool, si no está denominado en SOL, o si falla la
        lectura (red, RPC caído, etc.) — nunca tira excepción hacia
        arriba, para que el bot pueda seguir esperando el feed en vivo
        en vez de caerse por un problema de este fallback secundario."""
        price, _pool_confirmed_absent = await self.fetch_price_or_confirm_absent(mint)
        return price

    async def fetch_price_or_confirm_absent(self, mint: str) -> tuple[Optional[float], bool]:
        """Igual que `fetch_price_for_migrated_mint`, pero además devuelve
        `pool_confirmed_absent`: True ÚNICAMENTE cuando el
        getProgramAccounts para este mint respondió sin tirar excepción
        y no encontró ningún pool -es decir, una confirmación limpia de
        que el mint TODAVÍA NO migró a PumpSwap (sigue en bonding
        curve). En cualquier otro caso (se encontró un pool pero no se
        pudo leer/parsear, no está denominado en SOL, o la consulta
        on-chain en sí falló) devuelve False, porque ahí no hay ninguna
        confirmación real de que el mint no haya migrado -puede ser un
        problema genuino de RPC, no un mint sin pool- así que el
        llamador no debería asumir que conviene reintentar el feed en
        vivo."""
        async with AsyncClient(self.rpc_url) as client:
            try:
                pool_address = await self._find_pool_address(client, mint)
                if pool_address is None:
                    logger.debug(f"[On-chain PumpSwap] No se encontró ningún pool de PumpSwap para {mint}.")
                    return None, True

                pool_keys, _pool_type = await fetch_pool_state(pool_address, client)
                if pool_keys is None:
                    logger.debug("[On-chain PumpSwap] No se pudo leer/parsear la cuenta del pool.")
                    return None, False

                if pool_keys.get("quote_mint") != WSOL_MINT:
                    logger.debug(f"[On-chain PumpSwap] El pool de {mint} no está denominado en SOL "
                                 f"(quote_mint={pool_keys.get('quote_mint')}); no lo puedo usar acá.")
                    return None, False

                result = await fetch_pool_base_price(pool_keys, client)
                if result is None:
                    logger.debug("[On-chain PumpSwap] No se pudieron leer las reservas del pool.")
                    return None, False

                price, base_balance, quote_balance = result
                if not base_balance or float(price) <= 0:
                    return None, False

                logger.debug(f"[On-chain PumpSwap] Pool {pool_address} | reservas: "
                             f"{base_balance} tokens / {quote_balance} SOL")
                return float(price), False
            except Exception as e:
                logger.warning(f"[On-chain PumpSwap] Falló la consulta on-chain para {mint}: {e}")
                return None, False

    @classmethod
    def _pool_quote_mint(cls, data: bytes) -> Optional[str]:
        """quote_mint del pool, leído del account data crudo."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        raw = data[cls._QUOTE_MINT_OFFSET:cls._QUOTE_MINT_OFFSET + 32]
        try:
            return str(Pubkey.from_bytes(raw))
        except Exception:
            return None

    @classmethod
    def _pool_lp_supply(cls, data: bytes) -> Optional[int]:
        """lp_supply del pool (u64 little-endian), leído del account data
        crudo."""
        if data is None or len(data) < cls._MIN_POOL_ACCOUNT_LEN:
            return None
        return int.from_bytes(data[cls._LP_SUPPLY_OFFSET:cls._LP_SUPPLY_OFFSET + 8], "little")

    async def _find_pool_address(self, client: AsyncClient, mint: str) -> Optional[str]:
        """getProgramAccounts sobre el programa de PumpSwap, filtrando por
        `base_mint == mint` con un memcmp en el offset exacto del struct.
        Si hay varios pools para el mismo mint (raro, pero el struct
        soporta `index`), nos quedamos con el de mayor `lp_supply` (el
        pool "real" con liquidez, no uno vacío/de prueba).

        BUGFIX: la selección entre candidatos llamaba a fetch_pool_state()
        por cada uno, y esa función hace su PROPIO getAccountInfo -o sea,
        un round-trip de RPC extra por candidato, justo en el camino que
        ya es el que se come el rate limit. El account data crudo ya viene
        en `acc.account.data` de este mismo getProgramAccounts, así que
        ahora se parsea localmente: cero llamadas extra. De paso se
        descartan acá los pools que no están denominados en SOL, en vez de
        elegir el de mayor liquidez y recién después descubrir que no
        sirve."""
        resp = await client.get_program_accounts(
            Pubkey.from_string(PUMPSWAP_PROGRAM_ID),
            encoding="base64",
            filters=[MemcmpOpts(offset=self._BASE_MINT_OFFSET, bytes=mint)],
        )
        accounts = resp.value
        if not accounts:
            return None
        if len(accounts) == 1:
            return str(accounts[0].pubkey)

        best_pubkey = None
        best_lp_supply = -1
        descartados_por_quote = 0
        for acc in accounts:
            data = getattr(getattr(acc, "account", None), "data", None)
            quote_mint = self._pool_quote_mint(data)
            if quote_mint is not None and quote_mint != WSOL_MINT:
                descartados_por_quote += 1
                continue
            lp_supply = self._pool_lp_supply(data)
            if lp_supply is None:
                # Cuenta más corta de lo esperado (¿layout nuevo?): no la
                # descartamos, pero solo la usamos si no hay nada mejor.
                lp_supply = 0
            if lp_supply > best_lp_supply:
                best_lp_supply = lp_supply
                best_pubkey = str(acc.pubkey)

        if descartados_por_quote:
            logger.debug(f"[On-chain PumpSwap] {len(accounts)} pools para {mint}; "
                         f"{descartados_por_quote} descartados por no estar denominados en SOL.")
        return best_pubkey