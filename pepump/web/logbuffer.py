"""
Los últimos logs del proceso, en memoria, para mostrarlos en la web.

Es un logging.Handler más (al lado del de la terminal): guarda cada
registro como un dict simple -con su mint, ver log_context.py- en una
cola circular, y se lo pasa en el momento a las conexiones WebSocket
abiertas. No escribe en disco: al reiniciar el proceso se pierde, igual
que la salida de la terminal.
"""
import asyncio
import logging
import threading
from collections import deque
from typing import List, Optional, Set, Tuple

from pepump.log_context import MintContextFilter
from pepump.redact import SecretRedactingFilter


class LogBuffer(logging.Handler):
    # Por cliente: si un navegador no lee (pestaña colgada), se descartan
    # sus logs en vez de acumular memoria sin límite.
    _LISTENER_QUEUE_MAX = 1000

    def __init__(self, maxlen: int = 2000, level: int = logging.INFO):
        super().__init__(level=level)
        self.addFilter(MintContextFilter())
        # Lo que se guarda aquí lo ve cualquiera que entre al panel.
        self.addFilter(SecretRedactingFilter())
        self._entries: deque = deque(maxlen=maxlen)
        self._seq = 0
        # emit() también lo llaman los hilos de asyncio.to_thread.
        self._lock = threading.Lock()
        self._listeners: Set[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                # exc_text: el traceback ya tapado por SecretRedactingFilter.
                message += "\n" + (record.exc_text or logging.Formatter().formatException(record.exc_info))
            with self._lock:
                self._seq += 1
                entry = {
                    "seq": self._seq,
                    "ts": record.created,
                    "level": record.levelname,
                    "mint": getattr(record, "mint", ""),
                    "logger": record.name,
                    "message": message,
                }
                self._entries.append(entry)
                listeners = list(self._listeners)
            for loop, queue in listeners:
                try:
                    loop.call_soon_threadsafe(_put_or_drop, queue, entry)
                except RuntimeError:
                    pass  # loop ya cerrado
        except Exception:
            self.handleError(record)

    def entries(self, mint: Optional[str] = None, after: int = 0, limit: int = 500) -> List[dict]:
        with self._lock:
            items = [e for e in self._entries
                     if e["seq"] > after and (mint is None or e["mint"] == mint)]
        return items[-limit:]

    def subscribe(self) -> asyncio.Queue:
        """Cola por la que llegan los logs nuevos (llamar desde el loop)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._LISTENER_QUEUE_MAX)
        with self._lock:
            self._listeners.add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._listeners = {(lp, q) for lp, q in self._listeners if q is not queue}


def _put_or_drop(queue: asyncio.Queue, entry: dict) -> None:
    try:
        queue.put_nowait(entry)
    except asyncio.QueueFull:
        pass
