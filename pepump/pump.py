import requests
import websockets
import json
import logging
import asyncio
import time
from typing import AsyncIterator, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey  # type: ignore
from solders.signature import Signature  # type: ignore
from pumpswapamm.pumpswapamm import fetch_pool_state
from pumpswapamm.fetch_reserves import fetch_pool_base_price

logger = logging.getLogger(__name__)

# Programa de PumpSwap en Solana (constante pública, no cambia).
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Mint del SOL "wrapped" (WSOL) — para confirmar que el pool que
# encontramos está denominado en SOL antes de usar su precio.
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Códigos de error "custom" del programa spl-token (spl_token::error::TokenError),
# estables y públicos. Como `pool = "auto"` puede terminar rutando la compra/venta
# por distintos programas (bonding curve de pump.fun, PumpSwap, Raydium, etc.), no
# tiene sentido mantener una tabla por-programa -pero casi cualquier ruta termina
# haciendo un CPI a spl-token para mover el WSOL o el token, así que estos códigos
# son los que más se ven en la práctica cuando revienta una compra/venta real.
_SPL_TOKEN_CUSTOM_ERRORS = {
    0: "NotRentExempt (la cuenta quedaría por debajo del mínimo exento de rent)",
    1: "InsufficientFunds (fondos insuficientes para cubrir la transferencia — "
       "revisa el balance de SOL/WSOL o del token en la wallet)",
    2: "InvalidMint",
    3: "MintMismatch (el mint de la cuenta no coincide con el esperado)",
    4: "OwnerMismatch (la cuenta no pertenece al owner esperado)",
    5: "FixedSupply",
    6: "AlreadyInUse",
    7: "InvalidNumberOfProvidedSigners",
    8: "InvalidNumberOfRequiredSigners",
    9: "UninitializedState",
    10: "NativeNotSupported",
    11: "NonNativeHasBalance",
    12: "InvalidInstruction",
    13: "InvalidState",
    14: "Overflow",
    15: "AuthorityTypeNotSupported",
    16: "MintCannotFreeze",
    17: "AccountFrozen",
    18: "MintDecimalsMismatch",
    19: "NonNativeNotSupported",
}

# Versión corta (una línea, sin la explicación entre paréntesis) de la
# tabla de arriba, para el mensaje de error PRINCIPAL -la explicación
# larga queda solo en el log de debug, no hace falta repetirla siempre.
_SPL_TOKEN_CUSTOM_ERRORS_SHORT = {
    code: msg.split(" (", 1)[0] for code, msg in _SPL_TOKEN_CUSTOM_ERRORS.items()
}


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


async def _fetch_relevant_program_logs(signature: str, rpc_url: str) -> list[str]:
    """Pide la transacción completa (getTransaction) para sacar sus
    logMessages -que casi siempre incluyen la razón real y en texto
    plano de por qué revirtió un programa (ej. un AnchorError con
    "Error Message: ..." o el motivo exacto de spl-token)- y devuelve
    solo las líneas que parecen relevantes (mencionan error/fallo).
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
        if not logs:
            return []
        keywords = ("error", "Error", "fail", "Fail", "insufficient", "Insufficient",
                    "slippage", "Slippage", "revert", "exceed", "Exceed")
        relevant = [line for line in logs if any(k in line for k in keywords)]
        return relevant[-5:] if relevant else []
    except Exception as e:
        logger.debug(f"[Confirmación on-chain] no se pudieron leer los logs de {signature} "
                     f"para un mensaje de error más claro: {e}")
        return []


async def _describe_onchain_error(signature: str, rpc_url: str, err) -> tuple[str, str]:
    """Devuelve (razón_corta, detalle_técnico):
      - razón_corta: UNA frase legible para meter directo en el mensaje
        de error principal (ej. ": fondos insuficientes en la wallet
        (spl-token InsufficientFunds, instrucción #3)"). Vacía si no se
        pudo determinar nada mejor que el error crudo.
      - detalle_técnico: el error crudo de solders + los logs relevantes
        de la transacción, para loguear aparte a nivel DEBUG -no hace
        falta ensuciar el mensaje principal con esto, pero conviene
        tenerlo a mano con -v para casos raros/no reconocidos."""
    decoded = _extract_instruction_error(err)
    logs = await _fetch_relevant_program_logs(signature, rpc_url)

    debug_lines = [f"error crudo: {err}"]
    if logs:
        debug_lines.append("logs relevantes del programa:\n  " + "\n  ".join(logs))
    detail = "\n  ".join(debug_lines)

    if decoded is not None:
        ix_index, code = decoded
        short = _SPL_TOKEN_CUSTOM_ERRORS_SHORT.get(code)
        if short:
            return f": {short} (spl-token, instrucción #{ix_index})", detail

    if logs:
        # Sin código de spl-token conocido, pero SÍ hay algún log
        # relevante (típicamente el motivo real que imprime el propio
        # programa que revirtió, ej. un AnchorError con "Error Message:
        # ...") -usamos la línea más específica (la última) como razón.
        return f": {logs[-1].strip()}", detail

    return ": revirtió con un error no reconocido (corré con -v para ver más detalle, o abrí el link de Solscan)", detail


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

            def _token_amount_for_wallet(balances) -> Optional[float]:
                if not balances:
                    return None
                for b in balances:
                    if b.mint == mint and b.owner == wallet:
                        ui_amount = b.ui_token_amount.ui_amount
                        return float(ui_amount) if ui_amount is not None else 0.0
                return None

            pre_tokens = _token_amount_for_wallet(meta.pre_token_balances)
            post_tokens = _token_amount_for_wallet(meta.post_token_balances)
            # Si el mint no aparece en ninguno de los dos lados es que no
            # pudimos identificar el movimiento de tokens (rareza en el
            # formato de respuesta del RPC) -no asumimos 0 en ese caso.
            if pre_tokens is None and post_tokens is None:
                return None
            token_delta = (post_tokens or 0.0) - (pre_tokens or 0.0)

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
                yield json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

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
        """
        v_sol = event.get("vSolInBondingCurve")
        v_tok = event.get("vTokensInBondingCurve")
        if v_sol is not None and v_tok is not None and v_tok:
            return v_sol / v_tok

        market_cap_sol = event.get("marketCapSol")
        if market_cap_sol is not None:
            try:
                price = float(market_cap_sol) / cls.TOTAL_SUPPLY_TOKENS
                if price > 0:
                    return price
            except (TypeError, ValueError):
                pass

        sol_amount = event.get("solAmount")
        token_amount = event.get("tokenAmount")
        if sol_amount is not None and token_amount is not None:
            try:
                sol_amount = float(sol_amount)
                token_amount = float(token_amount)
                if token_amount > 0:
                    return sol_amount / token_amount
            except (TypeError, ValueError):
                pass

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

    # Offset en bytes de `base_mint` dentro de la cuenta del pool,
    # verificado contra el struct real de pumpswapamm
    # (PumpSwapPoolStateNew/Old en pumpswapamm.py):
    #   8 (discriminador Anchor) + 1 (pool_bump) + 2 (index) + 32 (creator)
    _BASE_MINT_OFFSET = 43

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

    async def _find_pool_address(self, client: AsyncClient, mint: str) -> Optional[str]:
        """getProgramAccounts sobre el programa de PumpSwap, filtrando por
        `base_mint == mint` con un memcmp en el offset exacto del struct.
        Si hay varios pools para el mismo mint (raro, pero el struct
        soporta `index`), nos quedamos con el de mayor `lp_supply` (el
        pool "real" con liquidez, no uno vacío/de prueba)."""
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

        # Más de un pool para el mismo mint: nos quedamos con el de mayor
        # lp_supply comparando el account data crudo (evita otro round-trip
        # de RPC por cada candidato).
        best_pubkey = None
        best_lp_supply = -1
        for acc in accounts:
            try:
                pool_keys, _ = await fetch_pool_state(acc.pubkey, client)
                lp_supply = (pool_keys or {}).get("lp_supply", 0)
                if lp_supply > best_lp_supply:
                    best_lp_supply = lp_supply
                    best_pubkey = str(acc.pubkey)
            except Exception:
                continue
        return best_pubkey