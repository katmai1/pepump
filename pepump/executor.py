from dataclasses import dataclass, field


@dataclass
class Position:
    mint: str
    entry_price: float                 # precio en SOL por token, al comprar
    sol_amount: float                  # SOL invertidos
    token_amount: float                # tokens obtenidos (estimado)
    highest_price: float = field(init=False)
    armed: bool = False                # ¿ya se activó el trailing stop?
    closed: bool = False

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

    def buy(self, mint: str, price: float) -> Position:
        sol_amount = self.cfg.buy_sol

        if self.live:
            try:
                result = self.client.execute_lightning_trade(
                    action="buy", mint=mint, amount=sol_amount, denominated_in_sol=True,
                    slippage=self.cfg.slippage, priority_fee=self.cfg.priority_fee,
                    pool=self.cfg.pool,
                )
                print(f"[REAL] Compra enviada. Respuesta: {result}")
            except Exception as e:
                print(f"[REAL] Error al comprar: {e}")
                raise

        token_amount = sol_amount / price if price > 0 else 0.0
        etiqueta = "REAL" if self.live else "SIMULADO"
        print(f"[{etiqueta}] COMPRA de {sol_amount} SOL en {mint} a precio {price:.10f} SOL/token "
              f"(~{token_amount:,.2f} tokens)")
        return Position(mint=mint, entry_price=price, sol_amount=sol_amount, token_amount=token_amount)

    def sell(self, position: Position, price: float, reason: str) -> None:
        if self.live:
            try:
                result = self.client.execute_lightning_trade(
                    action="sell", mint=position.mint, amount="100%", denominated_in_sol=False,
                    slippage=self.cfg.slippage, priority_fee=self.cfg.priority_fee,
                    pool=self.cfg.pool,
                )
                print(f"[REAL] Venta enviada. Respuesta: {result}")
            except Exception as e:
                print(f"[REAL] Error al vender: {e}")

        pnl = position.pnl_pct(price)
        proceeds = position.token_amount * price
        etiqueta = "REAL" if self.live else "SIMULADO"
        print(f"[{etiqueta}] VENTA de {position.mint} a precio {price:.10f} SOL/token "
              f"| motivo: {reason} | PnL: {pnl:+.2f}% | SOL recibidos (aprox): {proceeds:.6f}")
        position.closed = True


