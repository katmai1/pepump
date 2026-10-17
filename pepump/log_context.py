"""
Etiqueta de posición en los logs.

TrailingTakeProfitBot.run() deja su mint en `current_mint` (una
ContextVar), y todo lo que se loguea desde ahí -el bot, el executor, el
cliente de PumpPortal, las tareas que crea y los hilos de
asyncio.to_thread, que heredan el contexto- sale con `record.mint`. Así,
con varias posiciones en el mismo proceso, cada línea de log se puede
atribuir a la suya sin pasarle el mint a cada logger.info().

`record.mint` vale "" para lo que se loguea fuera de un bot (arranque,
banner de run.py, etc.).
"""
import logging
from contextvars import ContextVar

current_mint: ContextVar[str] = ContextVar("pepump_current_mint", default="")


class MintContextFilter(logging.Filter):
    """Agrega `record.mint` a cada registro. Va en el HANDLER (no en un
    logger): los filtros de handler ven también los registros que
    propagan los loggers hijos (pepump.bot, pepump.executor...)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "mint"):
            record.mint = current_mint.get()
        return True


# Etiqueta legible por mint ("$SYM 7xKX"), la registra el bot cuando
# conoce el símbolo del token. Sin etiqueta, se usa el mint acortado.
_labels: dict = {}
# Las etiquetas solo se muestran con más de una posición (con una sola
# serían ruido). Lo activa el gestor en cuanto tiene la segunda, y ya no
# se apaga: así no cambia el formato a mitad de una sesión.
_tags_enabled = False


def enable_tags() -> None:
    global _tags_enabled
    _tags_enabled = True


def set_mint_label(mint: str, label: str) -> None:
    _labels[mint] = label


def mint_tag(mint: str) -> str:
    if not mint:
        return ""
    return _labels.get(mint) or (f"{mint[:4]}…{mint[-4:]}" if len(mint) > 10 else mint)


class MintTagFormatter(logging.Formatter):
    """Formatter que antepone "[etiqueta] " al mensaje de los logs de una
    posición. Se usa cuando hay varias en la misma terminal."""

    def format(self, record: logging.LogRecord) -> str:
        texto = super().format(record)
        tag = mint_tag(getattr(record, "mint", "")) if _tags_enabled else ""
        if not tag:
            return texto
        mensaje = record.getMessage()
        # Se inserta delante del mensaje (después de la hora y el nivel),
        # no al principio de la línea, para que las columnas sigan alineadas.
        i = texto.find(mensaje)
        if i < 0:
            return f"[{tag}] {texto}"
        return f"{texto[:i]}[{tag}] {texto[i:]}"
