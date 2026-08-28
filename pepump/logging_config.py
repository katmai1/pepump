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

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="[%(asctime)s][%(levelname)s]:\t %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # websockets, httpx y httpcore son re-verbosos en INFO/DEBUG (loguean
    # cada frame/request). Los bajamos a WARNING para no ensuciar la
    # salida del bot -pero SOLO si no estás en modo verbose (-v/DEBUG),
    # porque ahí sí queremos ver hasta el último detalle de conexión.
    if level > logging.DEBUG:
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
