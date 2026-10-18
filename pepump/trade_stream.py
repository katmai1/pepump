"""
Conexión de un bot al feed de trades de PumpPortal (subscribeTokenTrade):
conectar, esperar el siguiente evento compitiendo contra el cierre del bot
y un timeout, reconectar y cerrar.

`client` es un PumpPortalClient o un SharedTradeFeed (mismo interfaz:
connect_trade_stream / iter_trade_events).
"""
import asyncio
import logging
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


class ShutdownRequested(Exception):
    """Se pidió el cierre del bot (Ctrl+C/SIGTERM/close()) mientras se
    esperaba un evento del feed. No sale de bot.py."""


class TradeStream:
    def __init__(self, client, mint: str, shutdown: asyncio.Event):
        self.client = client
        self.mint = mint
        # El asyncio.Event de cierre del bot: next_event lanza
        # ShutdownRequested en cuanto se activa.
        self._shutdown = shutdown
        self.ws: Any = None
        self.events: Optional[AsyncIterator[dict]] = None
        # BUGFIX (generador cerrado por timeout): ver next_event. Tarea
        # pendiente de `self.events.__anext__()` que puede sobrevivir a un
        # timeout sin cancelarse, para poder seguir esperando el MISMO
        # evento en la próxima llamada en vez de cortar el generador async
        # subyacente.
        self._pending: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        """Conecta y deja el subscribe ya mandado (ver
        PumpPortalClient.connect_trade_stream). Propaga cualquier error."""
        self.ws = await self.client.connect_trade_stream(self.mint)
        self.events = self.client.iter_trade_events(self.ws)

    async def reconnect(self) -> None:
        """Cierra la conexión vieja (sin quejarse si ya estaba rota) y abre
        una nueva. Propaga el error si no se puede conectar."""
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        await self.connect()
        # El generador viejo quedó abandonado de verdad aquí (nueva
        # conexión, no un timeout): su __anext__() pendiente ya no sirve.
        self._drop_pending()

    async def close(self) -> None:
        self._drop_pending()
        if self.ws is not None:
            # Un fallo al cerrar (socket ya roto del otro lado) no puede
            # reventar el cierre del bot: la excepción saldría de run()
            # DESPUÉS de una operación que salió bien.
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"No se pudo cerrar limpiamente el websocket: {e}")

    def _drop_pending(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    async def next_event(self, timeout: Optional[float] = None) -> dict:
        """__anext__() de self.events, pero compitiendo contra
        `_shutdown_requested` (y, si se pasa `timeout`, contra un
        deadline). Lanza ShutdownRequested si gana el apagado,
        asyncio.TimeoutError si gana el timeout, o deja pasar cualquier
        excepción normal del feed (StopAsyncIteration, errores de red,
        etc.).

        BUGFIX: antes, cuando ganaba el timeout, se cancelaba
        directamente la tarea que envolvía `self.events.__anext__()`.
        Cancelar esa tarea lanza un CancelledError DENTRO del generador
        async en su punto de espera (ej. el `await websocket.recv()`
        interno de iter_trade_events) -y como nada lo atrapa ahí dentro,
        el generador queda CERRADO para siempre: cualquier __anext__()
        posterior sobre el mismo generador devuelve StopAsyncIteration
        de inmediato, aunque la conexión siga perfectamente viva. Esto rompía
        en silencio cualquier código que esperara poder seguir
        escuchando el mismo feed después de un timeout (ver
        bot._get_reference_price reintentando tras confirmar 'sin pool', y
        _consume_trade_stream retomando el feed tras un stall sin
        migración real).

        Ahora, si gana el timeout, NO se cancela la tarea: se guarda en
        self._pending para reutilizarla en la próxima
        llamada -mismo generador, mismo __anext__() en vuelo, sin
        cortar nada-. Solo se cancela de verdad si gana el shutdown
        (ahí sí termina todo)."""
        # BUGFIX (evento perdido tras un stall): si la tarea que quedó
        # pendiente de un timeout anterior YA terminó, su resultado es un
        # trade real que llegó mientras hacíamos otra cosa (típicamente
        # las consultas RPC de bot._handle_feed_stall, que tardan). Antes se
        # descartaba y se creaba un __anext__() nuevo: ese trade se
        # perdía -y con él, una evaluación del trailing-stop y el reset
        # de bot._curve_stalls-. Si terminó con excepción (conexión
        # cerrada), .result() la propaga aquí y el llamador reconecta, en
        # vez de quedar como "Task exception was never retrieved".
        pending = self._pending
        if pending is not None and pending.done() and not pending.cancelled():
            self._pending = None
            if self._shutdown.is_set():
                # Se pidió apagado mientras tanto: no devolvemos un
                # precio que dispararía una compra justo al salir.
                raise ShutdownRequested()
            return pending.result()

        if pending is None or pending.cancelled():
            if self.events is None:
                raise RuntimeError("TradeStream.next_event sin conectar")
            next_task = asyncio.ensure_future(self.events.__anext__())
        else:
            next_task = pending
        shutdown_task = asyncio.ensure_future(self._shutdown.wait())
        try:
            done, _pending = await asyncio.wait(
                {next_task, shutdown_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # Cancelaron a quien esperaba (run() cancela el monitor de
            # precios al terminar): el feed ya no se va a leer más, así que
            # aquí SÍ se cancela el __anext__ en vuelo. Si se dejara
            # colgado, terminaría solo al cerrarse el feed con una
            # excepción que nadie recoge ("Task exception was never
            # retrieved").
            next_task.cancel()
            shutdown_task.cancel()
            self._pending = None
            raise

        if shutdown_task in done:
            next_task.cancel()
            self._pending = None
            raise ShutdownRequested()

        shutdown_task.cancel()

        if next_task in done:
            self._pending = None
            return next_task.result()  # puede propagar StopAsyncIteration u otra excepción

        # Ganó el timeout: dejamos next_task VIVA (sin cancelar) para
        # retomarla en la próxima llamada en vez de cerrar el generador.
        self._pending = next_task
        raise TimeoutError()
