import datetime
import logging
import time
from dataclasses import dataclass, field

from pepump.history import append_closed_trade

logger = logging.getLogger(__name__)


@dataclass
class Position:
    mint: str
    entry_price: float                 # precio en SOL por token, al comprar
    sol_amount: float                  # SOL invertidos
    token_amount: float                # tokens obtenidos
    highest_price: float = field(init=False)
    armed: bool = False                # ¿ya se activó el trailing stop?
    closed: bool = False
    # True si entry_price/sol_amount/token_amount salen de los balances
    # REALES pre/post de la transacción de compra confirmada (ver
    # TradeExecutor.buy) en vez de ser una estimación a partir del
    # precio de referencia. Solo puede ser True en modo REAL, y solo si
    # se pudo leer la tx (ver pump.py:_fetch_actual_fill) -si esa
    # lectura falla, se cae al estimado y esto queda en False aunque la
    # compra haya sido real.
    entry_is_real_fill: bool = False
    # time.time() del momento en que se abrió la posición (Position se crea
    # recién al comprar -ver TradeExecutor.buy-, así que esto es "ahora"
    # salvo que se pise explícitamente, como hacen los tests). Sirve para
    # calcular cuánto duró abierta la posición en el historial de CSV.
    opened_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.highest_price = self.entry_price

    def pnl_pct(self, price: float) -> float:
        return (price / self.entry_price - 1.0) * 100.0


# --------------------------------------------------------------------------- #
# Ejecutor de trades: decide y ejecuta compra/venta, simulada o real
# --------------------------------------------------------------------------- #

class TradeExecutor:
    """
    Gestiona la compra y venta de un token, ya sea en modo SIMULADO (no toca
    la red, solo lleva la cuenta en memoria) o en modo REAL (usa un
    PumpPortalClient para mandar la orden a la Lightning API).
    """

    def __init__(self, client, live: bool, config):
        self.client = client
        self.live = live
        self.cfg = config

    async def buy(self, mint: str, price: float, pool_override: str = None) -> Position:
        # Valores ESTIMADOS a partir del precio de referencia -se usan
        # tal cual en modo SIMULADO, y como respaldo en modo REAL si no
        # se pudieron leer los datos reales de fill (ver más abajo).
        sol_amount = self.cfg.buy_sol
        entry_price = price
        token_amount = sol_amount / price if price > 0 else 0.0
        real_fill = False

        if self.live:
            try:
                result = await self.client.execute_lightning_trade(
                    action="buy", mint=mint, amount=sol_amount, denominated_in_sol=True,
                    slippage=self.cfg.slippage, priority_fee=self.cfg.priority_fee,
                    pool=pool_override or self.cfg.pool,
                    solana_rpc_url=self.cfg.solana_rpc_url,
                    tx_confirm_timeout_seconds=self.cfg.tx_confirm_timeout_seconds,
                    tx_confirm_poll_interval_seconds=self.cfg.tx_confirm_poll_interval_seconds,
                )
                logger.info(f"[REAL] Compra CONFIRMADA on-chain. Respuesta: {result}")
            except Exception:
                # execute_lightning_trade ahora confirma en cadena antes
                # de devolver: si esto tira, es porque la orden fue
                # rechazada de una, o la tx confirmó pero FALLÓ on-chain
                # (p. ej. slippage excedido), o no confirmó a tiempo. En
                # NINGUNO de esos casos hay que abrir una posición -no se
                # compró nada de verdad-, así que propagamos el error y
                # no seguimos de largo.
                logger.exception("[REAL] Error al comprar (la orden no se confirmó on-chain)")
                raise

            # La compra CONFIRMÓ on-chain. execute_lightning_trade ahora
            # devuelve, además de la respuesta cruda de la Lightning
            # API, los datos REALES de fill (leídos de los balances
            # pre/post de la propia transacción) en "actual_sol_delta" /
            # "actual_token_delta" -ver pump.py:_fetch_actual_fill. Si
            # están, reemplazan la estimación con los números reales de
            # lo que efectivamente pasó en la wallet (incluye fees).
            actual_sol_delta = result.get("actual_sol_delta") if isinstance(result, dict) else None
            actual_token_delta = result.get("actual_token_delta") if isinstance(result, dict) else None
            if actual_sol_delta is not None and actual_token_delta is not None and actual_token_delta > 0:
                real_sol_spent = abs(actual_sol_delta)  # gastamos SOL -> sol_delta viene negativo
                real_token_amount = actual_token_delta
                sol_amount = real_sol_spent
                token_amount = real_token_amount
                entry_price = real_sol_spent / real_token_amount
                real_fill = True
                logger.info(f"[REAL] Datos REALES de la compra (de la tx confirmada, incluyen fees): "
                            f"gastaste {real_sol_spent:.9f} SOL y recibiste {real_token_amount:,.6f} "
                            f"tokens -> precio efectivo real: {entry_price:.10f} SOL/token.")
            else:
                logger.warning(f"[REAL] No se pudieron confirmar los datos reales de la compra; "
                                f"se usa el ESTIMADO (precio de referencia {price:.10f} SOL/token, "
                                f"~{token_amount:,.2f} tokens) para la posición.")

        etiqueta = "REAL" if self.live else "SIMULADO"
        sufijo = " [datos reales]" if real_fill else (" [estimado]" if self.live else "")
        logger.info(f"[{etiqueta}] COMPRA de {sol_amount:.9f} SOL en {mint} a precio "
                    f"{entry_price:.10f} SOL/token (~{token_amount:,.2f} tokens){sufijo}")
        return Position(mint=mint, entry_price=entry_price, sol_amount=sol_amount,
                         token_amount=token_amount, entry_is_real_fill=real_fill)

    async def sell(self, position: Position, price: float, reason: str, pool_override: str = None) -> None:
        # Valores ESTIMADOS a partir del precio de referencia -se usan
        # tal cual en modo SIMULADO, y como respaldo en modo REAL si no
        # se pudieron leer los datos reales de fill (ver más abajo).
        exit_price = price
        token_amount_sold = position.token_amount
        proceeds = token_amount_sold * price
        real_fill = False

        if self.live:
            try:
                result = await self.client.execute_lightning_trade(
                    action="sell", mint=position.mint, amount="100%", denominated_in_sol=False,
                    slippage=self.cfg.slippage, priority_fee=self.cfg.priority_fee,
                    pool=pool_override or self.cfg.pool,
                    solana_rpc_url=self.cfg.solana_rpc_url,
                    tx_confirm_timeout_seconds=self.cfg.tx_confirm_timeout_seconds,
                    tx_confirm_poll_interval_seconds=self.cfg.tx_confirm_poll_interval_seconds,
                )
                logger.info(f"[REAL] Venta CONFIRMADA on-chain. Respuesta: {result}")
            except Exception:
                # BUGFIX: antes esta excepción se logueaba y se seguía de
                # largo igual: la posición se marcaba `closed = True` y se
                # imprimía el PnL/proceeds como si la venta hubiese salido
                # bien, cuando en realidad la Lightning API falló (o la tx
                # confirmó pero FALLÓ on-chain, p. ej. por slippage
                # excedido) y el token real seguía en la wallet, sin
                # vender. Con SOL real eso es peligroso: el bot "cree" que
                # cerró la posición y deja de vigilarla. Ahora propagamos
                # la excepción y NO marcamos la posición como cerrada,
                # para que quede claro que la venta real falló y haya que
                # reintentarla (bot.py reintenta solo con el próximo
                # precio que llegue — ver _try_sell).
                logger.exception(
                    f"[REAL] Error al vender {position.mint} (la orden no se confirmó on-chain). "
                    "La posición SIGUE ABIERTA: no se marcó como vendida."
                )
                raise

            # La venta CONFIRMÓ on-chain. Igual que en buy(), reemplazamos
            # la estimación con los datos REALES de fill si se pudieron
            # leer -ver pump.py:_fetch_actual_fill.
            actual_sol_delta = result.get("actual_sol_delta") if isinstance(result, dict) else None
            actual_token_delta = result.get("actual_token_delta") if isinstance(result, dict) else None
            if actual_sol_delta is not None and actual_token_delta is not None and actual_sol_delta > 0:
                real_sol_received = actual_sol_delta  # recibimos SOL -> sol_delta viene positivo
                real_token_amount_sold = abs(actual_token_delta)  # vendimos tokens -> viene negativo
                proceeds = real_sol_received
                token_amount_sold = real_token_amount_sold
                exit_price = real_sol_received / real_token_amount_sold if real_token_amount_sold > 0 else price
                real_fill = True
                logger.info(f"[REAL] Datos REALES de la venta (de la tx confirmada, ya netos de fees): "
                            f"vendiste {real_token_amount_sold:,.6f} tokens y recibiste "
                            f"{real_sol_received:.9f} SOL -> precio efectivo real: {exit_price:.10f} "
                            f"SOL/token.")
            else:
                logger.warning(f"[REAL] No se pudieron confirmar los datos reales de la venta; "
                                f"se usa el ESTIMADO (precio de referencia y token_amount de la "
                                f"posición) para el PnL.")

        # PnL calculado en SOL real gastado vs. SOL real recibido -no
        # como ratio de precios-, para que en modo REAL con datos reales
        # (real_fill=True) refleje EXACTAMENTE lo que pasó en la wallet
        # (compra y venta ya vienen netas de fees en ese caso). En modo
        # SIMULADO o si no hay datos reales, position.sol_amount y
        # proceeds son ambos estimados a partir de precios, y da el
        # mismo resultado que el cálculo anterior basado en precios.
        pnl_sol = proceeds - position.sol_amount
        pnl_pct = (pnl_sol / position.sol_amount * 100.0) if position.sol_amount > 0 else 0.0
        etiqueta = "REAL" if self.live else "SIMULADO"
        sufijo = " [datos reales]" if real_fill else (" [estimado]" if self.live else "")
        logger.info(f"[{etiqueta}] VENTA de {position.mint} a precio {exit_price:.10f} SOL/token "
                    f"| motivo: {reason} | PnL: {pnl_pct:+.2f}% ({pnl_sol:+.9f} SOL) "
                    f"| SOL recibidos: {proceeds:.9f}{sufijo}")
        position.closed = True

        # Historial de órdenes cerradas: se registra DESPUÉS de marcar
        # closed = True (la venta ya está confirmada -real u obligada a
        # "éxito" en modo simulado-, así que solo faltaría persistir el
        # registro). Un problema al escribir el CSV nunca debe hacer
        # parecer que la venta falló -ver append_closed_trade, que absorbe
        # sus propios errores de disco.
        history_path = getattr(self.cfg, "trade_history_csv", "") or ""
        append_closed_trade(history_path, {
            "closed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "mint": position.mint,
            "mode": etiqueta,
            "entry_price": f"{position.entry_price:.10f}",
            "exit_price": f"{exit_price:.10f}",
            "sol_amount": f"{position.sol_amount:.9f}",
            "token_amount": f"{token_amount_sold:.6f}",
            "proceeds_sol": f"{proceeds:.9f}",
            "pnl_pct": f"{pnl_pct:.4f}",
            "duration_seconds": f"{max(time.time() - position.opened_at, 0.0):.1f}",
            "reason": reason,
        })


