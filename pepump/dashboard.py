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


def create_panel(stream) -> Optional[StatusPanel]:
    """StatusPanel si `stream` es una terminal que entiende ANSI, si no None."""
    try:
        is_tty = stream.isatty()
    except (AttributeError, ValueError):
        return None
    if not is_tty or os.environ.get("TERM") == "dumb":
        return None
    if not _enable_windows_vt(stream):
        return None
    return StatusPanel(stream)
