import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configura el logging de toda la app (llamar UNA sola vez, al arrancar
    run.py). El resto de los módulos solo hace `logging.getLogger(__name__)`
    y no toca handlers ni formatters.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Evita duplicar handlers si setup_logging() se llama más de una vez
    # (por ejemplo en tests).
    if root.handlers:
        return

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
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        # stdout no es un TextIOWrapper (lo pisó pytest, un wrapper
        # propio, etc.). No es crítico: en Linux ya es UTF-8, y en
        # Windows sobre consola también.
        pass

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="[%(asctime)s][%(levelname)s]:\t %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # websockets, httpx y httpcore son muy verbosos en INFO/DEBUG (loguean
    # cada frame/request). Los bajamos a WARNING para no ensuciar la
    # salida del bot -pero SOLO si no estás en modo verbose (-v/DEBUG),
    # porque ahí sí queremos ver hasta el último detalle de conexión.
    if level > logging.DEBUG:
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
