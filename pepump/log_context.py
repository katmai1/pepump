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
