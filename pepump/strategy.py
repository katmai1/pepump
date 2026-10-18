"""
Estrategia de trailing take-profit, sin nada de I/O: recibe la posición y
un precio y dice qué hacer. Quien la usa (bot.py) aplica la decisión
-persistir, loguear, vender-. Así la regla se puede leer y probar sola, y
los umbrales se calculan en UN solo sitio (los usan también el panel, el
snapshot de la web y la línea de estado).

  1. Sin armar: si el precio sube `activation_pct` % desde la entrada, se
     arma el trailing; si baja `initial_stop_pct` %, stop-loss inicial.
  2. Armado: el nivel de venta sigue al máximo, `trailing_pct` % por
     debajo; si el precio lo toca, se vende.

Contra qué precio de entrada se miden los % de (1) lo decide
`price_basis` (ver Position.strategy_entry_price): "real" usa lo que se
pagó de verdad, con comisiones; "market", el precio de mercado al
comprar. El máximo y el trailing van siempre sobre precios de mercado.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pepump.executor import Position


class Action(Enum):
    HOLD = "hold"
    ARM = "arm"            # se armó el trailing (el máximo arranca en este precio)
    NEW_HIGH = "new_high"  # trailing armado y nuevo máximo
    SELL = "sell"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""

    @property
    def is_sell(self) -> bool:
        return self.action is Action.SELL


HOLD = Decision(Action.HOLD)


@dataclass(frozen=True)
class TrailingStrategy:
    activation_pct: float
    trailing_pct: float
    initial_stop_pct: float
    price_basis: str = "real"

    @classmethod
    def from_config(cls, cfg) -> "TrailingStrategy":
        return cls(cfg.activation_pct, cfg.trailing_pct, cfg.initial_stop_pct,
                   cfg.strategy_price_basis)

    # ---- Umbrales ---------------------------------------------------------- #

    def entry_price(self, pos: Position) -> float:
        return pos.strategy_entry_price(self.price_basis)

    def activation_price(self, pos: Position) -> float:
        return self.entry_price(pos) * (1 + self.activation_pct / 100.0)

    def initial_stop_price(self, pos: Position) -> float:
        return self.entry_price(pos) * (1 - self.initial_stop_pct / 100.0)

    def trailing_stop_price(self, pos: Position) -> Optional[float]:
        """Nivel de venta del trailing; None mientras no esté armado."""
        if not pos.armed:
            return None
        return pos.highest_price * (1 - self.trailing_pct / 100.0)

    def pnl_pct(self, pos: Position, price: float) -> float:
        """El % que corresponde a entry_price: neto con "real", de mercado
        con "market"."""
        return pos.strategy_pnl_pct(price, self.price_basis)

    # ---- Decisión ---------------------------------------------------------- #

    def evaluate(self, pos: Position, price: float) -> Decision:
        """Qué hacer con `pos` ante un precio nuevo. No toca la posición:
        con ARM/NEW_HIGH el llamador actualiza `armed`/`highest_price`."""
        if pos.closed:
            return HOLD

        if not pos.armed:
            if price >= self.activation_price(pos):
                return Decision(Action.ARM)
            if price <= self.initial_stop_price(pos):
                return Decision(Action.SELL, "stop-loss inicial (nunca se activó el trailing)")
            return HOLD

        if price > pos.highest_price:
            return Decision(Action.NEW_HIGH)
        if price <= pos.highest_price * (1 - self.trailing_pct / 100.0):
            return Decision(Action.SELL, f"retroceso de {self.trailing_pct}% desde el máximo "
                                         f"({pos.highest_price:.10f})")
        return HOLD
