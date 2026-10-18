"""
Feed de trades COMPARTIDO: un único websocket a PumpPortal para todas las
posiciones del proceso.

PumpPortal pide usar UNA conexión y mandar varios `subscribeTokenTrade`
por ella, en vez de abrir una conexión por token (abrir muchas puede
acabar en un bloqueo). SharedTradeFeed hace eso y, hacia cada bot, se
comporta igual que PumpPortalClient en la parte del feed:

  - connect_trade_stream(mint) -> una suscripción "virtual" (no un
    websocket de verdad) para ese mint,
  - iter_trade_events(sub)     -> los eventos de ESE mint, nada más,
  - sub.close()                -> se da de baja (unsubscribeTokenTrade si
    era el último interesado en ese mint),
  - extract_price / DATA_WS_URL, delegados en el cliente real.

Así TrailingTakeProfitBot no cambia: no sabe si tiene una conexión propia
o una compartida.

Reconexión: si se cae el websocket real, el feed reconecta solo (con
backoff) y vuelve a suscribir todos los mints activos. Hacia los bots el
corte es invisible: solo ven un hueco sin trades, que es exactamente lo
que su propia lógica de "stall" ya sabe tratar (consulta on-chain,
fallbacks). Lo único que se le reporta a un bot es el fallo de la
PRIMERA conexión, igual que antes, para que pueda avisar y terminar.

El ack de suscripción ({"message": "Successfully subscribed..."}) no
dice a qué mint corresponde. El bot lo usa para saber que la
suscripción se aceptó, así que se reparte por orden: PumpPortal contesta
los subscribe en el orden en que se mandan, y el primer ack sin dueño va
para la suscripción más antigua que todavía lo esperaba. Los acks de las
re-suscripciones tras reconectar se descartan (esos bots ya tenían el
suyo).
"""
import asyncio
import contextvars
import json
import logging
from collections import deque
from typing import Any, AsyncIterator, Deque, Dict, Optional, Set

logger = logging.getLogger(__name__)

_END = object()  # marca de fin de una suscripción en su cola


class TradeSubscription:
    """Lo que recibe el bot en lugar del websocket: una cola con los
    eventos de su mint. close() equivale al ws.close() de antes."""

    def __init__(self, feed: "SharedTradeFeed", mint: str):
        self.feed = feed
        self.mint = mint
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        # Si ya se le entregó su ack de suscripción (ver SharedTradeFeed).
        self.acked = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.queue.put_nowait(_END)
        await self.feed._unsubscribe(self)


class SharedTradeFeed:
    _INITIAL_BACKOFF = 2.0
    _MAX_BACKOFF = 30.0

    def __init__(self, client, connect=None):
        """`client` es el PumpPortalClient real (de él salen la URL, la
        api_key, extract_price). `connect` permite inyectar la función que
        abre el websocket -los tests la reemplazan-; por defecto es
        websockets.connect."""
        self.client = client
        if connect is None:
            import websockets
            connect = websockets.connect  # type: ignore[attr-defined]
        self._connect = connect
        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        self._subs: Dict[str, Set[TradeSubscription]] = {}
        # Suscripciones esperando su ack, en el orden en que se mandó su
        # subscribe. None = un subscribe cuyo ack hay que descartar (una
        # re-suscripción tras reconectar).
        self._pending_acks: Deque[Optional[TradeSubscription]] = deque()
        # Serializa conectar/reconectar: dos bots arrancando a la vez no
        # pueden abrir dos websockets reales.
        self._conn_lock = asyncio.Lock()

    # --- misma interfaz que PumpPortalClient ------------------------------ #
    @property
    def DATA_WS_URL(self) -> str:
        return self.client.DATA_WS_URL

    def extract_price(self, event: dict) -> Optional[float]:
        return self.client.extract_price(event)

    async def connect_trade_stream(self, mint: str) -> TradeSubscription:
        """Suscribe `mint` por el websocket compartido (abriéndolo si es el
        primero). Si la conexión real no se puede abrir, lanza la excepción
        igual que PumpPortalClient.connect_trade_stream, para que el bot la
        reporte."""
        sub = TradeSubscription(self, mint)
        async with self._conn_lock:
            if self._ws is None:
                # Primera suscripción (o el feed estaba parado): conexión
                # nueva. Un fallo aquí sube al bot.
                self._ws = await self._open()
                # Contexto vacío: el reader no es de ninguna posición, así
                # que sus logs no heredan el mint del bot que lo arrancó.
                self._reader_task = asyncio.create_task(self._reader_loop(), name="pepump-shared-feed",
                                                        context=contextvars.Context())
            first_for_mint = mint not in self._subs
            self._subs.setdefault(mint, set()).add(sub)
            if first_for_mint:
                self._pending_acks.append(sub)
                await self._send_subscribe(mint)
            else:
                # Ya había otro interesado en el mismo mint (no debería
                # pasar con el gestor, que lo impide, pero el feed no lo
                # necesita): PumpPortal no manda otro ack, así que se le
                # da uno sintético para que el bot siga su camino normal.
                sub.acked = True
                sub.queue.put_nowait({"message": "Successfully subscribed to keys."})
        return sub

    @staticmethod
    async def iter_trade_events(sub: TradeSubscription) -> AsyncIterator[dict]:
        while True:
            event = await sub.queue.get()
            if event is _END:
                return
            yield event

    async def close(self) -> None:
        """Cierra todo (al apagar el proceso). Las suscripciones que
        queden reciben fin de stream."""
        for subs in list(self._subs.values()):
            for sub in list(subs):
                sub.closed = True
                sub.queue.put_nowait(_END)
        self._subs.clear()
        self._pending_acks.clear()
        await self._shutdown_connection()

    @property
    def active_mints(self) -> Set[str]:
        return set(self._subs)

    # --- internos ----------------------------------------------------------- #
    async def _open(self):
        url = self.client.DATA_WS_URL
        api_key = getattr(self.client, "api_key", "")
        if api_key:
            url = f"{url}?api-key={api_key}"
        else:
            logger.warning("Sin pumpportal.api_key configurada: subscribeTokenTrade NO va a entregar "
                           "ningún trade (requiere API key + wallet con >= 0.02 SOL, aunque el bot esté "
                           "en modo SIMULADO).")
        return await self._connect(url)

    async def _send_subscribe(self, mint: str) -> None:
        try:
            await self._ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
        except Exception as e:
            # La conexión se está cayendo: el reader lo va a notar y, al
            # reconectar, re-suscribe todo lo que esté en self._subs.
            logger.debug(f"[Feed compartido] no se pudo mandar el subscribe de {mint}: {e}")

    async def _unsubscribe(self, sub: TradeSubscription) -> None:
        async with self._conn_lock:
            await self._unsubscribe_locked(sub)

    async def _unsubscribe_locked(self, sub: TradeSubscription) -> None:
        subs = self._subs.get(sub.mint)
        if subs is None or sub not in subs:
            return
        subs.discard(sub)
        # Si todavía esperaba su ack, el lugar en la cola pasa a "descartar".
        for i, pendiente in enumerate(self._pending_acks):
            if pendiente is sub:
                self._pending_acks[i] = None
        if subs:
            return
        del self._subs[sub.mint]
        # El stream es MEDIDO (se paga por evento): darse de baja de
        # verdad, no solo dejar de leer.
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [sub.mint]}))
            except Exception as e:
                logger.debug(f"[Feed compartido] no se pudo mandar el unsubscribe de {sub.mint}: {e}")
        if not self._subs:
            # Nadie más escucha: se cierra la conexión real. La próxima
            # suscripción abre una nueva.
            await self._shutdown_connection()

    async def _shutdown_connection(self) -> None:
        reader, self._reader_task = self._reader_task, None
        ws, self._ws = self._ws, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception as e:
                logger.debug(f"[Feed compartido] error cerrando el websocket: {e}")

    def _dispatch(self, event: dict) -> None:
        mint = event.get("mint")
        if isinstance(mint, str) and mint:
            for sub in self._subs.get(mint, ()):
                sub.queue.put_nowait(event)
            return
        if set(event.keys()) == {"message"}:
            texto = str(event.get("message", "")).lower()
            if "unsubscribed" in texto:
                return  # ack de un unsubscribe: no es de nadie
            if self._pending_acks:
                pending = self._pending_acks.popleft()
                # None: ack de una re-suscripción de alguien que ya tenía
                # el suyo -> se descarta.
                if pending is not None and not pending.closed:
                    pending.acked = True
                    pending.queue.put_nowait(event)
            return
        # Errores u otros mensajes sin mint: no hay a quién atribuirlos.
        logger.debug(f"[Feed compartido] mensaje sin mint descartado: {event!r}")

    async def _reader_loop(self) -> None:
        backoff = self._INITIAL_BACKOFF
        while True:
            ws = self._ws
            try:
                async for raw_msg in ws:
                    try:
                        event = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    self._dispatch(event)
                    backoff = self._INITIAL_BACKOFF
                logger.warning("[Feed compartido] PumpPortal cerró la conexión.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[Feed compartido] conexión interrumpida: {e}")

            # Reconexión: solo si queda alguien escuchando.
            while True:
                if not self._subs:
                    async with self._conn_lock:
                        if self._reader_task is asyncio.current_task():
                            self._reader_task = None
                            self._ws = None
                    return
                mints = sorted(self._subs)
                logger.warning(f"[Feed compartido] reconectando en {backoff:.0f}s "
                               f"({len(mints)} token(s) suscriptos)...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._MAX_BACKOFF)
                async with self._conn_lock:
                    try:
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        self._ws = ws = await self._open()
                    except Exception as e:
                        logger.warning(f"[Feed compartido] no se pudo reconectar todavía: {e}")
                        continue
                    # Los acks que se esperaban en la conexión vieja ya no
                    # van a llegar: el de cada re-suscripción va para quien
                    # todavía no tenía el suyo (el bot lo necesita para
                    # arrancar su ventana de espera hacia el fallback
                    # on-chain), y se descarta si ya lo tenían todos.
                    self._pending_acks.clear()
                    for mint in sorted(self._subs):
                        sin_ack = next((x for x in self._subs[mint] if not x.acked), None)
                        self._pending_acks.append(sin_ack)
                        await self._send_subscribe(mint)
                logger.info(f"[Feed compartido] reconectado; re-suscriptos {len(self._subs)} token(s).")
                break

