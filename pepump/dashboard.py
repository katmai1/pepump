"""
Panel fijo al pie de la terminal: los datos que no cambian (mint, modo,
importe, activación, trailing, stop-loss...) quedan SIEMPRE visibles
abajo, y la línea de estado (precio, PnL) se reescribe en su sitio en
vez de agregar una línea nueva cada `status_interval_seconds`. Los logs
normales (compra, armado, nuevo máximo, venta, errores) siguen saliendo
por encima del panel, como siempre.

Cómo funciona: el panel se dibuja SIN salto de línea final. Antes de
escribir cualquier log, se borra (se sube el cursor las líneas que ocupa
y se limpia hasta el final de la pantalla con ANSI), se escribe el log,
y se vuelve a dibujar debajo. Por eso cada línea del panel se recorta al
ancho de la terminal: si una línea se partiera en dos, la cuenta de
líneas a borrar quedaría mal y el panel "dejaría basura" en pantalla.

Solo se activa si stdout es una terminal de verdad (y en Windows, si se
pudo habilitar el modo VT de la consola). Redirigido a un archivo o a un
pipe, el bot se comporta exactamente como antes: líneas de estado
periódicas normales en el log.
"""
import logging
import os
import shutil
import threading
from typing import List, Optional


def _enable_windows_vt(stream) -> bool:
    """En Windows hay que pedirle a la consola que interprete los códigos
    ANSI (ENABLE_VIRTUAL_TERMINAL_PROCESSING); Windows Terminal ya lo trae,
    pero conhost clásico no. Si no se puede, el panel se desactiva."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class StatusPanel:
    def __init__(self, stream):
        self.stream = stream
        self._fixed: List[str] = []
        self._status: str = ""
        self._drawn = 0  # cuántas líneas del panel hay ahora en pantalla
        # RLock y no asyncio: también escriben logs los hilos de
        # asyncio.to_thread (POST a la Lightning API, etc.).
        self._lock = threading.RLock()

    # --- API para el bot ------------------------------------------------ #
    def set_fixed(self, lines: List[str]) -> None:
        with self._lock:
            self._fixed = list(lines)
            self._redraw()

    def set_status(self, line: str) -> None:
        with self._lock:
            self._status = line
            self._redraw()

    def set_all(self, fixed: List[str], status: str) -> None:
        """Las dos cosas de una vez, con un solo redibujado."""
        with self._lock:
            self._fixed = list(fixed)
            self._status = status
            self._redraw()

    def close(self) -> None:
        """Al salir: se deja el bloque fijo impreso (sin la línea de
        estado, que ya no es "en vivo") y se baja el cursor para que el
        prompt de la shell no lo pise."""
        with self._lock:
            self._status = ""
            self._redraw()
            if self._drawn:
                self.stream.write("\n")
                self.stream.flush()
            self._drawn = 0

    # --- dibujo --------------------------------------------------------- #
    def _lines(self) -> List[str]:
        if not self._fixed and not self._status:
            return []
        width = shutil.get_terminal_size((100, 24)).columns
        sep = "─" * max(10, width - 1)
        lines = [sep, *self._fixed, sep]
        if self._status:
            lines.append(self._status)
        # -2: escribir justo en la última columna hace que algunas
        # terminales salten de línea solas, y algún emoji (⏳) ocupa dos
        # columnas contando como un solo carácter.
        return [ln[:width - 2] for ln in lines]

    def clear(self) -> None:
        with self._lock:
            if self._drawn:
                self.stream.write("\r" + "\x1b[1A" * (self._drawn - 1) + "\x1b[J")
                self.stream.flush()
                self._drawn = 0

    def draw(self) -> None:
        with self._lock:
            lines = self._lines()
            if lines:
                self.stream.write("\n".join(lines))
                self.stream.flush()
            self._drawn = len(lines)

    def _redraw(self) -> None:
        self.clear()
        self.draw()


class PanelAwareHandler(logging.StreamHandler):
    """StreamHandler que borra el panel antes de escribir cada log y lo
    vuelve a dibujar debajo, así el panel siempre queda al pie."""

    def __init__(self, stream, panel: StatusPanel):
        super().__init__(stream)
        self.panel = panel

    def emit(self, record: logging.LogRecord) -> None:
        with self.panel._lock:
            self.panel.clear()
            super().emit(record)
            self.panel.draw()


def panel_unavailable_reason(stream) -> Optional[str]:
    """None si `stream` es una terminal que entiende ANSI; si no, el
    motivo, para avisarlo en el log en vez de desactivar el panel en
    silencio."""
    try:
        is_tty = stream.isatty()
    except (AttributeError, ValueError):
        is_tty = False
    if not is_tty:
        return "la salida no es una terminal (¿redirigida a un archivo o a un pipe?)"
    if os.environ.get("TERM") == "dumb":
        return "TERM=dumb"
    if not _enable_windows_vt(stream):
        return "la consola de Windows no admite códigos ANSI (modo VT)"
    return None


def create_panel(stream) -> Optional[StatusPanel]:
    """StatusPanel si `stream` es una terminal que entiende ANSI, si no None."""
    if panel_unavailable_reason(stream) is not None:
        return None
    return StatusPanel(stream)


# --------------------------------------------------------------------------- #
# Varias posiciones en la misma terminal (ver manager.py)
# --------------------------------------------------------------------------- #

_FASES = {
    "starting": "arrancando",
    "connecting": "conectando",
    "waiting_entry": "esperando entrada",
    "buying": "comprando",
    "open": "abierta",
    "closing": "cerrando",
    "closed": "cerrada",
    "stopped": "terminada",
}


def short_mint(mint: str) -> str:
    return f"{mint[:4]}…{mint[-4:]}" if len(mint) > 10 else mint


def position_label(snap: dict) -> str:
    """"$SYM 7xKX" si ya se conoce el símbolo, si no el mint acortado."""
    corto = snap["mint"][:4]
    if snap.get("token_symbol"):
        return f"${snap['token_symbol']} {corto}"
    return short_mint(snap["mint"])


def position_row(snap: dict) -> str:
    """Una línea por posición para el panel con varias posiciones."""
    partes = [f" #{snap['id']} {position_label(snap)}" + (" (retomada)" if snap.get("recovered") else "")]
    fase = _FASES.get(snap["phase"], snap["phase"])
    pos = snap.get("position")
    if pos is not None and snap["phase"] == "open":
        fase += " · armada" if pos["armed"] else " · esperando activación"
    if snap["phase"] == "open" and not snap["running"] and snap.get("detached"):
        fase = "⏸️  detenida sin vender"
    elif snap["phase"] == "open" and not snap["running"]:
        fase = "⚠️  ABIERTA SIN VIGILAR"
    elif snap["close_requested"] and snap["phase"] not in ("closed", "stopped"):
        fase = "cerrando"
    partes.append(fase)

    if pos is not None and pos["closed"]:
        if pos.get("realized_pnl_pct") is not None:
            partes.append(f"PnL {pos['realized_pnl_pct']:+.2f}% ({pos['realized_pnl_sol']:+.6f} SOL)")
        if pos.get("close_reason"):
            partes.append(pos["close_reason"])
    elif pos is not None:
        if snap["latest_price"] is not None:
            partes.append(f"precio {snap['latest_price']:.10f}")
        if pos["armed"] and pos["trailing_stop_price"] is not None:
            partes.append(f"venta ≤ {pos['trailing_stop_price']:.10f}")
        else:
            partes.append(f"activa ≥ {pos['activation_price']:.10f}")
        if pos["market_pnl_pct"] is not None:
            pnl = f"mercado {pos['market_pnl_pct']:+.2f}%"
            if pos["entry_is_real_fill"] and pos["net_pnl_pct"] is not None:
                pnl += f" neto {pos['net_pnl_pct']:+.2f}%"
            partes.append(pnl)
    else:
        if snap["latest_price"] is not None:
            partes.append(f"precio {snap['latest_price']:.10f}")
        if snap.get("entry_target_price") is not None:
            partes.append(f"objetivo {snap['entry_target_price']:.10f}")
        if snap["phase"] == "stopped" and snap.get("end_reason"):
            partes.append(snap["end_reason"])
    return " | ".join(partes)


class _PositionPanel:
    """Lo que recibe cada bot como `panel` cuando corre bajo el gestor:
    guarda su bloque fijo y su línea de estado (las mismas que dibujaría
    él solo) y le avisa al panel compartido que redibuje."""

    def __init__(self, owner: "MultiPositionPanel"):
        self._owner = owner
        self.fixed: List[str] = []
        self.status: str = ""

    def set_fixed(self, lines: List[str]) -> None:
        self.fixed = list(lines)
        self._owner.refresh()

    def set_status(self, line: str) -> None:
        self.status = line
        self._owner.refresh()


class MultiPositionPanel:
    """Panel de la terminal con el gestor de posiciones.

    Con UNA sola posición se ve exactamente como siempre (el bloque fijo
    y la línea de estado de ese bot). Con varias, una cabecera y una
    línea por posición (ver position_row)."""

    def __init__(self, panel: StatusPanel, snapshot: callable, live: bool):
        self.panel = panel
        self._snapshot = snapshot
        self._live = live
        self._children: dict = {}
        self.ctrl_c_hint = "Ctrl+C sale sin vender"

    def for_position(self, managed) -> _PositionPanel:
        child = _PositionPanel(self)
        self._children[managed.id] = child
        return child

    def refresh(self) -> None:
        snaps = self._snapshot()
        if len(snaps) == 1 and snaps[0]["id"] in self._children:
            child = self._children[snaps[0]["id"]]
            self.panel.set_all(child.fixed, child.status)
            return
        modo = "REAL" if self._live else "SIMULADO"
        activas = sum(1 for s in snaps if s["running"])
        cabecera = [f" PePump | Modo {modo} | {len(snaps)} posiciones ({activas} activas) "
                    f"| {self.ctrl_c_hint}"]
        self.panel.set_all(cabecera + [position_row(s) for s in snaps], "")

    def close(self) -> None:
        self.panel.close()
