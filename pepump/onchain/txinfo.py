"""Qué pasó con una transacción ya enviada: por qué falló on-chain (mensaje
legible) y cuánto SOL/tokens movió de verdad en la wallet (fill real)."""
import asyncio
import logging
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.signature import Signature

from pepump.onchain.errors import describe_custom_error, failing_program_from_logs

logger = logging.getLogger(__name__)


def _extract_instruction_error(err) -> Optional[tuple]:
    """Si `err` (el `.err` de getSignatureStatuses) es un
    TransactionErrorInstructionError con un código Custom(N), devuelve
    (índice_de_instrucción, código). None si no matchea esa forma (otro
    tipo de error, versión distinta de solders, etc.) -nunca lanza
    excepción, esto es solo para dar un mensaje más claro, no crítico."""
    try:
        from solders.transaction_status import (
            InstructionErrorCustom,
            TransactionErrorInstructionError,
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
    onchain/errors.py: el mismo número significa cosas distintas en
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


async def describe_onchain_error(signature: str, rpc_url: str, err) -> tuple[str, str]:
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
    (línea "Program <id> failed" de los logs) y solo entonces se traduce el
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

    return (": revirtió con un error no reconocido (ejecuta con -v para ver más detalle, "
            "o abre el enlace de Solscan)"), detail


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
    "usa la cuenta de otro". El atajo de la única entrada queda
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


async def fetch_actual_fill(signature: str, rpc_url: str, mint: str,
                              max_attempts: int = 3, retry_delay_seconds: float = 0.75,
                              wallet: Optional[str] = None) -> Optional[dict]:
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
    Con `wallet` se busca esa dirección entre las cuentas de la tx en vez de
    suponer el índice 0: en una orden de Jupiter enrutada por un market
    maker, el que paga la tx puede no ser nuestra wallet.

    Devuelve None si por lo que sea no se puede leer/parsear la tx (el
    llamador debe caer entonces al precio/importe ESTIMADO en vez de
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
    desfase real entre "el status ya dice confirmed/finalized" y "ya
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

            account_keys = resp.value.transaction.transaction.message.account_keys  # type: ignore[union-attr]
            if not account_keys:
                return None
            keys = [str(k) for k in account_keys]
            if wallet is not None and wallet not in keys:
                return None
            index = keys.index(wallet) if wallet is not None else 0
            payer = keys[index]

            sol_delta = (meta.post_balances[index] - meta.pre_balances[index]) / 1_000_000_000.0

            # Movimiento real de tokens de NUESTRA wallet -ver
            # _wallet_token_delta, que es donde vive la identificación de
            # cuál de las token accounts de la tx es la nuestra (y el
            # bugfix por el que las compras caían siempre al estimado).
            token_delta = _wallet_token_delta(meta, mint, payer)
            if token_delta is None:
                return None

            return {"sol_delta": sol_delta, "token_delta": token_delta, "wallet": payer}
        except Exception as e:
            if attempt < max_attempts:
                await asyncio.sleep(retry_delay_seconds)
                continue
            logger.warning(f"No se pudieron leer los datos reales de fill de {signature} "
                            f"(se va a usar el estimado como respaldo, tras {max_attempts} intentos): {e}")
            return None
    return None  # inalcanzable (el loop siempre retorna o sigue), queda por claridad
