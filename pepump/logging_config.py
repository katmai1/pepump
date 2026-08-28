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

    # websockets es re-verboso en INFO/DEBUG (loguea cada frame); lo bajamos
    # a WARNING para no ensuciar la salida del bot.
    logging.getLogger("websockets").setLevel(logging.WARNING)
