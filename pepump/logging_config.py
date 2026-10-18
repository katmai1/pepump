import logging
import sys
from typing import Optional

from pepump.dashboard import PanelAwareHandler, StatusPanel, create_panel, panel_unavailable_reason
from pepump.log_context import MintContextFilter, MintTagFormatter


def setup_logging(level: int = logging.INFO, panel: bool = True,
                  tag_mint: bool = False) -> Optional[StatusPanel]:
    """
    Configura el logging de toda la app (llamar UNA sola vez, al arrancar
    run.py). El resto de los módulos solo hace `logging.getLogger(__name__)`
    y no toca handlers ni formatters.

    Devuelve el StatusPanel (panel fijo al pie, ver dashboard.py) si
    `panel` es True y stdout es una terminal; si no, None y la salida es
    la de siempre, línea a línea.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Evita duplicar handlers si setup_logging() se llama más de una vez
    # (por ejemplo en tests).
    if root.handlers:
        return None

    # Los logs llevan emoji (⚠️ del modo real, ⏱️ del estado, ✅/📈 del
    # trailing-stop). En Windows, cuando la salida NO es la consola
    # -`> log.txt`, un pipe-, Python no usa UTF-8 sino la codificación
    # local (cp1252 en un Windows en español): cada línea con emoji
    # revienta con UnicodeEncodeError, logging lo captura e imprime
    # "--- Logging error ---" en stderr, y EL MENSAJE SE PIERDE. O sea
    # que redirigir a un archivo se tragaba justo las líneas de estado y
    # de armado/venta del trailing-stop. Se fuerza UTF-8, y errors=
    # "replace" como red de seguridad para que ninguna salida rara pueda
    # hacer desaparecer una línea de log.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        # stdout no es un TextIOWrapper (lo pisó pytest, un wrapper
        # propio, etc.). No es crítico: en Linux ya es UTF-8, y en
        # Windows sobre consola también.
        pass

    status_panel = create_panel(sys.stdout) if panel else None
    no_panel_reason = panel_unavailable_reason(sys.stdout) if panel and status_panel is None else None
    handler: logging.Handler
    if status_panel is not None:
        handler = PanelAwareHandler(sys.stdout, status_panel)
    else:
        handler = logging.StreamHandler(sys.stdout)
    # Con varias posiciones en la misma terminal, cada línea lleva la
    # etiqueta de su token ("[$SYM 7xKX] ...") para saber de cuál es.
    formatter = (MintTagFormatter if tag_mint else logging.Formatter)(
        fmt="[%(asctime)s][%(levelname)s]:\t %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    # `record.mint` en cada log (ver log_context.py). Solo se muestra con
    # tag_mint (varias posiciones): con una sola sería ruido.
    handler.addFilter(MintContextFilter())
    root.addHandler(handler)

    if no_panel_reason:
        logging.getLogger(__name__).info(f"Panel fijo desactivado: {no_panel_reason}. "
                                         f"El estado se loguea como una línea más.")

    # websockets, httpx y httpcore son muy verbosos en INFO/DEBUG (loguean
    # cada frame/request). Los bajamos a WARNING para no ensuciar la
    # salida del bot -pero SOLO si no estás en modo verbose (-v/DEBUG),
    # porque ahí sí queremos ver hasta el último detalle de conexión.
    if level > logging.DEBUG:
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        # Servidor del panel web (--web): solo avisos y errores.
        logging.getLogger("uvicorn").setLevel(logging.WARNING)

    return status_panel
