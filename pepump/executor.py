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
    token_amount: float                # tokens obtenidos (estimado)
    highest_price: float = field(init=False)
    armed: bool = False                # ¿ya se activó el trailing stop?
    closed: bool = False
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
        sol_amount = self.cfg.buy_sol

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

        token_amount = sol_amount / price if price > 0 else 0.0
        etiqueta = "REAL" if self.live else "SIMULADO"
        logger.info(f"[{etiqueta}] COMPRA de {sol_amount} SOL en {mint} a precio {price:.10f} SOL/token "
                    f"(~{token_amount:,.2f} tokens)")
        return Position(mint=mint, entry_price=price, sol_amount=sol_amount, token_amount=token_amount)

    async def sell(self, position: Position, price: float, reason: str, pool_override: str = None) -> None:
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

        pnl = position.pnl_pct(price)
        proceeds = position.token_amount * price
        etiqueta = "REAL" if self.live else "SIMULADO"
        logger.info(f"[{etiqueta}] VENTA de {position.mint} a precio {price:.10f} SOL/token "
                    f"| motivo: {reason} | PnL: {pnl:+.2f}% | SOL recibidos (aprox): {proceeds:.6f}")
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
            "exit_price": f"{price:.10f}",
            "sol_amount": f"{position.sol_amount:.9f}",
            "token_amount": f"{position.token_amount:.6f}",
            "proceeds_sol": f"{proceeds:.9f}",
            "pnl_pct": f"{pnl:.4f}",
            "duration_seconds": f"{max(time.time() - position.opened_at, 0.0):.1f}",
            "reason": reason,
        })


