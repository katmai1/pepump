import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Callable, Optional

from pepump.executor import Position, sell_retry_params
from pepump.jupiter import JUPITER_POOL, PRIORITY_BACKGROUND
from pepump.log_context import current_mint, set_mint_label
from pepump.onchain.fallback import OnchainFallback
from pepump.strategy import Action, TrailingStrategy
from pepump.trade_stream import ShutdownRequested, TradeStream

logger = logging.getLogger(__name__)

# Cada cuánto tiempo (segundos), mientras seguimos esperando el primer
# trade real, se repite el recordatorio de diagnóstico con las causas
# más probables (wallet sin fondos, api_key inválida, token sin volumen).
_DIAGNOSTIC_REMINDER_SECONDS = 10.0

# Cuántos "stalls" SEGUIDOS del feed en vivo (sin migración a PumpSwap,
# con la bonding curve todavía activa) se toleran antes de dejar de
# confiar en subscribeTokenTrade para esta posición y pasar a polling
# on-chain. Uno solo puede ser una pausa normal de volumen; varios
# seguidos significan que el feed no está entregando nada para este
# mint aunque el token siga vivo -y mientras tanto el trailing-stop
# solo se evalúa una vez cada `stall_timeout_seconds`, que con el
# default son 10s de ceguera por precio. Se reinicia en cuanto vuelve
# a llegar un trade real (ver _consume_trade_stream).
_MAX_CURVE_STALLS_BEFORE_POLLING = 2

# Muestreo de precios para el gráfico de la web (ver _record_price): como
# mucho un punto cada _PRICE_SAMPLE_SECONDS, salvo que el precio se mueva
# más de _PRICE_SAMPLE_JUMP desde el último punto guardado (un pico o una
# caída brusca no se pueden perder entre muestras).
_PRICE_SAMPLE_SECONDS = 2.0
_PRICE_SAMPLE_JUMP = 0.03
# Puntos que se guardan en memoria por posición (~11 h a un punto cada 2 s).
_PRICE_HISTORY_MAX = 20_000

# Mientras la comprobación temprana de existencia on-chain (ver
# OnchainFallback.confirm_not_listed / _get_reference_price) todavía no terminó,
# cada espera de un trade del feed en vivo se corta en rebanadas de
# esta duración en vez de esperar de golpe todo `live_feed_timeout_seconds`
# -así, apenas esa comprobación confirma que el mint no es de pump.fun, el
# bot aborta en el próximo ciclo del loop en vez de solo después de
# los live_feed_timeout_seconds completos (pensados para tolerar
# tokens de poco volumen, no para esto).
_EXISTENCE_CHECK_POLL_SECONDS = 0.5

# Con cuántas ventas fallidas SEGUIDAS se avisa en ERROR (y en la web) de
# que hay que mirar la posición: a partir de ahí lo normal es que algo
# más esté mal (sin SOL para fees, RPC caído, token sin liquidez...).
_SELL_FAILURES_ALERT = 3

# Cada cuánto se registra el precio tras la venta si sale de Jupiter (ver
# follow_after_close): cada consulta gasta el cupo de peticiones de la cuenta.
_FOLLOW_JUPITER_INTERVAL_SECONDS = 30.0


class TrailingTakeProfitBot:
    """
    Orquesta todo: escucha el feed de precios vía PumpPortalClient y decide,
    a través de un TradeExecutor, cuándo comprar y cuándo vender según la
    lógica de trailing take-profit / stop-loss inicial.
    """

    def __init__(self, client, executor, config, panel=None, metadata_client=None,
                 persist=None, resume_position: Optional[Position] = None,
                 resume_onchain_source: Optional[str] = None, jupiter=None):
        self.client = client
        self.executor = executor
        self.mint = config.mint
        self.cfg = config
        self.status_interval_seconds = config.status_interval_seconds
        # Con resume_position, la posición ya está comprada (de una
        # ejecución anterior, ver PositionManager.recover): run() no compra,
        # solo la vigila.
        self.position: Optional[Position] = resume_position
        self.recovered = resume_position is not None
        # Historial de precios (ts, precio) para el gráfico; ver _record_price.
        self.price_history: deque = deque(maxlen=_PRICE_HISTORY_MAX)
        # Lo pone el gestor: guarda cada punto en la base de datos.
        self._price_cb: Optional[Callable[[float, float], None]] = None
        self._latest_price: Optional[float] = None
        self._closed_event = asyncio.Event()
        # Fallback on-chain (ver onchain/fallback.py): si el precio no sale
        # del feed en vivo, de dónde sale ahora (`self.onchain.source`) y
        # por qué pool hay que rutear la orden.
        self.onchain = OnchainFallback(self.mint, config.solana_rpc_url, resume_onchain_source,
                                       jupiter=jupiter, live=config.live,
                                       prefer_jupiter=config.jupiter_for_all)
        if resume_position is not None:
            self.onchain.jupiter_tokens = resume_position.token_amount
        # Si _get_reference_price (o algo que llame desde ahí) ya
        # logueó un motivo específico para devolver None -mint que no es
        # de pump.fun, timeout total, fallback on-chain sin precio,
        # conexión cortada, etc.-, se marca aquí para que el caller en
        # run() NO agregue ENCIMA el mensaje genérico de "se cortó la
        # conexión, verifica la dirección y la api_key": antes se
        # logueaban los dos, uno específico y después uno genérico que
        # podía contradecirlo (ej. "no es un token de pump.fun" seguido
        # de "verifica la api_key"), muy confuso para el usuario.
        self._initial_price_failure_reason_logged = False
        # Stalls SEGUIDOS del feed en vivo con la bonding curve todavía
        # activa (ver _handle_feed_stall y _MAX_CURVE_STALLS_BEFORE_POLLING).
        self._curve_stalls = 0
        # Se activa con close() -que llaman Ctrl+C (SIGINT), SIGTERM o
        # SIGBREAK vía shutdown.py, o quien controle el bot desde fuera-.
        # NO se usa el try/except
        # KeyboardInterrupt clásico porque en asyncio esa señal
        # interrumpe el loop de eventos "por afuera" de la corrutina en
        # ejecución, no dentro de ella -no hay garantía de que un
        # try/except puesto en el código de la app la agarre. Con un
        # manejador de señales que solo marca este asyncio.Event, el
        # apagado se coordina de forma confiable.
        self._shutdown_requested = asyncio.Event()
        # Conexión al feed de trades (ver trade_stream.py).
        self._stream = TradeStream(client, self.mint, self._shutdown_requested)
        # Motivo con el que se pidió close() (None si nadie lo pidió:
        # p. ej. la venta de cierre porque murió el monitor de precios).
        self._close_reason: Optional[str] = None
        # Qué hacer con una posición abierta al pedir el cierre: "sell"
        # (close(): se vende) o "keep" (stop(): se deja abierta y guardada
        # para retomarla al volver a arrancar).
        self._close_mode = "sell"
        # True si run() terminó dejando la posición abierta A PROPÓSITO (stop()).
        self.detached = False
        # Guarda el estado (ver store.py); lo pone el gestor. None = sin
        # persistencia (tests, uso suelto).
        self._persist_cb = persist
        # Estado para quien mire el bot desde fuera (ver snapshot()):
        #   "starting" -> "connecting" -> "waiting_entry" -> "buying"
        #   -> "open" -> "closing" (venta de cierre por close()) ->
        #   "closed" (posición vendida) o "stopped" (terminó sin abrir
        #   posición: cancelado, error de conexión, compra fallida...).
        # Si run() termina con la posición TODAVÍA abierta (falló la venta
        # de cierre) se queda en "open" con running=False: es el caso que
        # hay que revisar a mano.
        self.phase = "starting"
        self.running = False
        self.end_reason: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        # Precio objetivo de la baja de entrada (entry_dip_pct > 0), para
        # mostrarlo mientras se espera; None si no aplica o aún no se sabe.
        self._dip_target: Optional[float] = None
        # Precio de referencia contra el que se calculó ese objetivo.
        self._dip_reference: Optional[float] = None
        # BUGFIX (doble venta): serializa CUALQUIER intento de venta de la
        # posición (ya sea por trailing-stop/stop-loss vía _try_sell, o por
        # cierre manual vía _sell_on_shutdown). No alcanza con solo
        # reordenar la cancelación de tareas en run() para evitar la
        # carrera: execute_lightning_trade manda el POST real dentro de un
        # asyncio.to_thread, y cancelar la tarea que está esperando ese
        # await NO mata el hilo -el pedido HTTP ya en vuelo puede seguir
        # llegando al servidor de todos modos. Con este lock, si dos
        # caminos intentan vender casi al mismo tiempo, el segundo espera,
        # ve `pos.closed` ya en True (o el executor.sell lanza una excepción
        # porque no queda nada que vender) y no dispara un segundo pedido
        # real.
        self._sell_lock = asyncio.Lock()
        # Ventas fallidas SEGUIDAS de esta posición (ver _sell): cada una
        # sube el slippage y el priority fee del siguiente intento, y entre
        # intento e intento se espera un poco (_sell_retry_at, monotonic).
        self.sell_failures = 0
        self._sell_retry_at = 0.0
        # Panel fijo al pie de la terminal (ver dashboard.py). None cuando
        # la salida no es una terminal (archivo, pipe, tests): entonces el
        # estado se loguea como una línea más, igual que siempre.
        self.panel = panel
        # Nombre/símbolo del token, solo para mostrarlos (ver
        # _load_token_name). None si no se pasó cliente (tests) o si
        # todavía no llegaron / no se encontraron.
        self._metadata_client = metadata_client
        self.token_name: Optional[str] = None
        self.token_symbol: Optional[str] = None
        self._update_panel_fixed()

    @property
    def strategy(self) -> TrailingStrategy:
        return TrailingStrategy.from_config(self.cfg)

    def close(self, reason: str = "cierre manual") -> None:
        """Pide el cierre ordenado: si hay una posición abierta se vende
        al precio más actual posible y run() termina; si todavía no se
        compró, se corta la espera y run() termina sin comprar nada.

        Es la ÚNICA forma de parar el bot desde fuera (Ctrl+C vía
        shutdown.run_with_shutdown_signals, y más adelante la web). No
        bloquea ni espera a que se venda: solo marca el pedido; para
        saber cuándo terminó, se espera a run().

        `reason` sale en el log y en el motivo de la venta (historial)."""
        if self._shutdown_requested.is_set():
            if self._close_mode == "keep" and self.running:
                # Se había pedido parar sin vender y ahora piden cerrar
                # vendiendo (p. ej. "Cerrar" en la web mientras el proceso
                # se detenía): gana vender, si todavía se está a tiempo.
                self._close_mode = "sell"
                self._close_reason = reason
                logger.warning(f"⚠️  {reason}: se venderá en vez de dejarla abierta.")
            # Si no, segundo pedido (otro Ctrl+C) mientras ya se está
            # vendiendo/cerrando: no hacemos nada especial (no forzamos un
            # corte abrupto), simplemente evitamos loguear el aviso de nuevo.
            return
        # close() corre en el contexto de quien lo llama (el manejador de
        # la señal, la web), no en el de run(): se etiqueta a mano para
        # que este aviso salga con el mint del bot como el resto.
        ctx = current_mint.set(self.mint)
        try:
            logger.warning(f"⚠️  {reason}. Cerrando ordenadamente "
                           f"(si hay una posición abierta, se vende al precio actual)...")
        finally:
            current_mint.reset(ctx)
        self._close_reason = reason
        self._shutdown_requested.set()

    def stop(self, reason: str = "detenido") -> None:
        """Como close(), pero SIN vender: si hay una posición abierta se deja
        tal cual (guardada, se retoma al volver a arrancar) y run() termina.
        Si todavía no compró, cancela la entrada igual que close()."""
        if self._shutdown_requested.is_set():
            return
        ctx = current_mint.set(self.mint)
        try:
            if self.position is not None and not self.position.closed:
                logger.warning(f"⏸️  {reason}. La posición queda ABIERTA (sin vender) y se retoma al "
                               f"volver a arrancar el bot. Mientras esté parado, nadie vigila el stop.")
            else:
                logger.warning(f"⏸️  {reason}. Deteniendo...")
        finally:
            current_mint.reset(ctx)
        self._close_reason = reason
        self._close_mode = "keep"
        self._shutdown_requested.set()

    @property
    def latest_price(self) -> Optional[float]:
        return self._latest_price

    @latest_price.setter
    def latest_price(self, price: Optional[float]) -> None:
        # TODOS los caminos que actualizan el precio (feed en vivo, polling
        # on-chain, fallbacks, espera de la baja de entrada) pasan por aquí.
        self._latest_price = price
        if price is not None and price > 0:
            self._record_price(time.time(), price)

    def _record_price(self, ts: float, price: float) -> None:
        if self.price_history:
            last_ts, last_price = self.price_history[-1]
            if (ts - last_ts < _PRICE_SAMPLE_SECONDS
                    and abs(price / last_price - 1.0) < _PRICE_SAMPLE_JUMP):
                return
        self.price_history.append((ts, price))
        if self._price_cb is not None:
            try:
                self._price_cb(ts, price)
            except Exception as e:
                logger.debug(f"No se pudo guardar el punto de precio: {e!r}")

    def _persist(self) -> None:
        """Guarda el estado actual (ver store.py). Un fallo al guardar se
        avisa pero nunca tumba al bot: la posición sigue vigilada igual."""
        if self._persist_cb is None:
            return
        try:
            self._persist_cb(self)
        except Exception as e:
            logger.error(f"No se pudo guardar el estado de la posición: {e!r}. Si el bot se "
                         f"reinicia ahora, podría no retomarla.")

    async def run(self) -> None:
        """Corre el bot hasta que la posición se cierre (TP/SL), se pida
        close(), o no se pueda abrir. Ver _run para el detalle.

        Todo lo que se loguea mientras tanto -incluido lo que loguean el
        executor y el cliente, y los hilos de asyncio.to_thread- lleva el
        mint en `record.mint` (ver log_context.py), para poder separar los
        logs de cada posición cuando corran varias en el mismo proceso.

        OJO: cancelar la tarea de run() es un corte en seco, SIN vender.
        Para parar vendiendo la posición, close()."""
        ctx = current_mint.set(self.mint)
        self.running = True
        self.started_at = time.time()
        try:
            await self._run()
        finally:
            if self.position is not None and self.position.closed:
                self.phase = "closed"
            elif self.position is not None:
                self.phase = "open"
                if self.detached:
                    self.end_reason = "detenida sin vender: se retoma al volver a arrancar"
                else:
                    self.end_reason = "el bot terminó con la posición ABIERTA: revísala a mano"
            else:
                self.phase = "stopped"
            self.running = False
            self.finished_at = time.time()
            self._persist()
            current_mint.reset(ctx)

    async def _run(self) -> None:
        """
        Se suscribe (subscribe_trade) al mint ANTES de comprar, para tener
        el feed de precios en vivo corriendo desde el arranque. Con esa
        misma conexión ya abierta:
          1. espera el primer trade real del mint por ese feed y compra
             contra ese precio (ver _get_initial_price) — el precio de
             entrada SIEMPRE sale del feed en vivo, sin importar cuánto
             tarde: no hay ninguna otra fuente de precio,
          2. sigue escuchando ese mismo feed para reaccionar en tiempo real
             mientras dure la posición,
          3. en paralelo corre la impresión periódica del %% de profit.

        Ctrl+C (SIGINT), SIGTERM o Ctrl+Pausa (SIGBREAK, solo Windows) en
        cualquier momento: si ya hay una posición abierta, se vende al
        precio más actual posible antes de salir (ver _sell_on_shutdown);
        si todavía no se compró nada, simplemente corta la espera y
        termina sin vender nada. Las señales llegan aquí vía close(); ver
        shutdown.py para cómo se engancha cada una en Unix y en Windows.
        """
        logger.info(f"Siguiendo el token: {self.mint}")
        # En segundo plano: el nombre es solo informativo y no tiene por
        # qué retrasar la conexión al feed ni la compra.
        name_task = (asyncio.create_task(self._load_token_name())
                     if self._metadata_client is not None else None)
        logger.debug(f"Suscribiéndose (subscribe_trade) al feed de trades de PumpPortal para {self.mint}...")
        self.phase = "connecting"

        # OJO: connect_trade_stream ya deja el subscribe MANDADO del lado
        # de PumpPortal apenas conecta. Si algo revienta después de esto y
        # antes de que el finally pueda correr, la conexión queda
        # suscripta pero abandonada del lado del servidor (no se le avisa
        # con un cierre limpio de WebSocket, solo se corta el TCP cuando
        # el proceso muere). Por eso TODO lo que dependa del feed vive
        # dentro de este try/finally, sin excepciones: así cualquier
        # crash -incluso uno inesperado que no previmos- cierra el socket
        # de forma ordenada en vez de dejarlo zombie.
        try:
            # BUGFIX: antes, si connect_trade_stream fallaba (red caída,
            # DNS, api_key rechazada al nivel de handshake, etc.), la
            # excepción se escapaba sin capturar hasta fuera de run() ->
            # run.py solo atrapa KeyboardInterrupt, así que el bot moría
            # con un traceback crudo en vez de un mensaje claro. Ahora se
            # loguea el error y se sale ordenadamente (todavía no hay
            # posición abierta, así que no hay nada que vender).
            try:
                await self._stream.connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"No se pudo conectar/suscribir al feed de trades de PumpPortal "
                             f"({self.client.DATA_WS_URL}): {e}")
                self.end_reason = f"no se pudo conectar al feed de PumpPortal: {e}"
                return
            if self.position is None:
                if not await self._enter():
                    return
            else:
                # Posición retomada de una ejecución anterior (ver store.py y
                # PositionManager.recover): ya está comprada, se sigue vigilando
                # desde donde estaba (entrada, máximo, armado).
                self.phase = "open"
                pos = self.position
                estado = (f"armada, máximo {pos.highest_price:.10f}" if pos.armed
                          else f"esperando activación +{self.cfg.activation_pct}%")
                logger.info(f"♻️  Posición retomada: entrada {pos.market_entry_price:.10f} SOL/token, "
                            f"{pos.token_amount:,.2f} tokens, {estado}.")
            self._persist()

            if self.onchain.source is not None:
                # subscribeTokenTrade no está entregando nada útil para
                # este mint (ya migrado, o bonding curve pero el feed no
                # entrega precios) -> el monitoreo de la posición usa
                # polling on-chain en vez del consumidor del feed en vivo.
                monitor_task = asyncio.create_task(self._poll_onchain_price_loop())
            else:
                monitor_task = asyncio.create_task(self._consume_trade_stream())

            tasks = [
                monitor_task,
                asyncio.create_task(self._status_printer_loop()),
            ]

            # BUGFIX (carrera de doble venta): antes, _wait_for_close_or_shutdown
            # vendía DIRECTAMENTE al ganar el shutdown, mientras monitor_task
            # (_consume_trade_stream / _poll_onchain_price_loop) seguía vivo
            # y podía disparar su propio _try_sell si llegaba un precio que
            # cruzara el trailing-stop en esa misma ventana -> dos llamadas a
            # executor.sell() en simultáneo para la misma posición hacia la
            # Lightning API. Ahora _wait_for_close_or_shutdown SOLO espera y
            # devuelve si hace falta vender; monitor_task y el status printer
            # se cancelan aquí ANTES de vender, así que cuando corre
            # _sell_on_shutdown ya no hay nada más que pueda pisarle la venta.
            need_shutdown_sell = await self._wait_for_close_or_shutdown(monitor_task)

            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            if need_shutdown_sell and self._close_mode == "keep" \
                    and self.position is not None and not self.position.closed:
                # stop(): se deja abierta a propósito (ya avisó stop()).
                self.detached = True
            elif need_shutdown_sell:
                if self.position is not None and not self.position.closed:
                    self.phase = "closing"
                    logger.warning("Vendiendo la posición abierta al precio actual antes de salir...")
                    await self._sell_on_shutdown()
                else:
                    logger.info("No hay posición abierta; no hay nada que vender.")
        finally:
            if name_task is not None:
                name_task.cancel()
            await self._stream.close()

        logger.info("Bot finalizado.")

    async def _enter(self) -> bool:
        """Consigue el precio de entrada y compra. True si quedó una
        posición abierta; False si no (cancelado, sin precio, compra
        fallida), con self.end_reason explicando por qué."""
        self.phase = "waiting_entry"

        initial_price = await self._get_initial_price()
        if initial_price is None:
            if self._shutdown_requested.is_set():
                logger.info("Cancelado antes de abrir posición; no hay nada que vender.")
                self.end_reason = f"cancelado antes de abrir posición ({self._close_reason})"
                return False
            self.end_reason = "no se consiguió precio de entrada (ver logs)"
            if not self._initial_price_failure_reason_logged:
                # Solo llegamos aquí si no se logueó ningún motivo
                # específico arriba (ver _initial_price_failure_reason_logged):
                # un caso realmente no cubierto por los diagnósticos de
                # _get_reference_price. Si ya se logueó un motivo
                # puntual (mint que no es de pump.fun, timeout total,
                # fallback on-chain sin precio, etc.) NO lo repetimos aquí
                # con un mensaje genérico que podría contradecirlo.
                logger.error("Se cortó la conexión con PumpPortal antes de recibir un trade con "
                             "precio. Verifica la dirección del token y la api_key, y vuelve a intentar.")
            return False

        self.latest_price = initial_price
        self.phase = "buying"
        # ANTES de mandar la compra: si el proceso muere con la orden en
        # vuelo, al arrancar se sabe que hay que mirar la wallet.
        self._persist()
        try:
            await self._on_first_price(initial_price)
        except Exception as e:
            logger.error(f"La compra no se confirmó on-chain, no se abrió ninguna posición: {e}")
            self.end_reason = f"la compra falló: {e}"
            return False
        self.phase = "open"
        return True

    async def _wait_for_close_or_shutdown(self, monitor_task: Optional[asyncio.Task] = None) -> bool:
        """Espera a que la posición se cierre sola (TP/SL normal) O a que
        se pida un apagado (Ctrl+C/SIGTERM) O a que muera el monitor de
        precios.

        BUGFIX (cuelgue silencioso con la posición abierta): antes esto
        solo miraba `_closed_event` y `_shutdown_requested`. Si
        monitor_task (_consume_trade_stream / _poll_onchain_price_loop)
        se moría con una excepción inesperada, nadie se enteraba: la
        tarea queda en estado "done con exception" sin que nada la
        espere -asyncio ni siquiera lo loguea hasta que la recolecta el
        GC-, así que el bot se quedaba aquí esperando para siempre un
        evento de cierre que ya no podía llegar, con SOL real
        comprometido y sin vigilar el precio. La única salida era un
        Ctrl+C a mano. Ahora la tarea de monitoreo también entra en la
        espera: si termina antes de que la posición se cierre, se
        loguea el motivo y se sale vendiendo, que es lo seguro.

        A propósito NO vende aquí dentro (ver BUGFIX en run()): solo
        espera y devuelve si hace falta que run() dispare la venta de
        cierre, para que run() pueda cancelar primero monitor_task/
        status_printer_loop y evitar que ese monitor dispare su propia
        venta en simultáneo con la de shutdown.

        Devuelve True si hay que vender por shutdown (se pidió apagado y
        la posición no se cerró sola en esa misma carrera), False si la
        posición ya se cerró sola (TP/SL) y no hace falta hacer nada más.
        """
        closed_task = asyncio.ensure_future(self._closed_event.wait())
        shutdown_task = asyncio.ensure_future(self._shutdown_requested.wait())
        esperando = {closed_task, shutdown_task}
        if monitor_task is not None:
            esperando.add(monitor_task)
        try:
            await asyncio.wait(esperando, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # OJO: monitor_task NO se cancela aquí -de eso se encarga
            # run(), que lo cancela junto con el status printer ANTES de
            # disparar la venta de cierre (ver el BUGFIX de la doble venta).
            for t in (closed_task, shutdown_task):
                if not t.done():
                    t.cancel()

        if self._closed_event.is_set():
            return False  # se cerró sola (TP/SL), no hace falta hacer nada más

        if self._shutdown_requested.is_set():
            return True

        # No fue ni un cierre ni un Ctrl+C: se terminó el monitor de precios.
        if monitor_task is not None and monitor_task.done() and not monitor_task.cancelled():
            error = monitor_task.exception()
            if error is not None:
                logger.error(f"El monitor de precios se cortó con un error inesperado "
                             f"({error!r}). Ya no hay quién vigile el trailing-stop, así que "
                             f"cierro la posición al precio actual en vez de dejarla sin "
                             f"supervisión.")
            else:
                logger.error("El monitor de precios terminó sin que la posición se cerrara. "
                             "Cierro la posición al precio actual en vez de dejarla sin "
                             "supervisión.")
        return True

    _SHUTDOWN_SELL_ATTEMPTS = 3
    # Unidad de la pausa entre reintentos de venta (ver _sell); los tests
    # la ponen en 0.
    _SELL_RETRY_DELAY_SECONDS = 1.0

    async def _sell_on_shutdown(self) -> None:
        """Intenta conseguir el precio MÁS actual posible (una consulta
        on-chain fresca si veníamos usando ese fallback; si no, el último
        precio que ya venía actualizando el feed en vivo, que está
        prácticamente en tiempo real) y vende de inmediato la posición
        abierta.

        Usa _sell_lock (ver __init__) para no pisarse con un _try_sell
        del monitor que haya quedado en vuelo. Revalida `position.closed`
        DESPUÉS de conseguir el lock: si _try_sell ya vendió mientras
        esperábamos aquí, no hace falta (ni corresponde) vender de nuevo."""
        async with self._sell_lock:
            if self.position is None or self.position.closed:
                logger.info("No hay posición abierta; no hay nada que vender.")
                return
            # Se reintenta: al cerrar no hay "próximo precio" que dispare
            # otro intento como en _try_sell, y un solo timeout de la
            # Lightning API dejaba la posición abierta y el bot ya fuera.
            for _attempt in range(self._SHUTDOWN_SELL_ATTEMPTS):
                price = await self._resolve_shutdown_price()
                if price is None or price <= 0:
                    logger.error("No se pudo determinar ningún precio para vender al cerrar. La posición "
                                 f"queda ABIERTA — revísala manualmente: https://pump.fun/{self.mint}")
                    return
                wait = self._sell_retry_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                if await self._sell(self.position, price, self._shutdown_sell_reason()):
                    self._persist()
                    return
            logger.error(f"Falló la venta de cierre tras {self._SHUTDOWN_SELL_ATTEMPTS} intentos. "
                         f"La posición SIGUE ABIERTA — revísala manualmente: https://pump.fun/{self.mint}")

    def _shutdown_sell_reason(self) -> str:
        if self._close_reason is not None:
            return f"cierre manual ({self._close_reason})"
        # Nadie pidió close(): se vende porque murió el monitor de precios
        # (ver _wait_for_close_or_shutdown).
        return "monitor de precios caído"

    async def _resolve_shutdown_price(self) -> Optional[float]:
        if self.onchain.source is not None:
            try:
                price = await self.onchain.fetch_price()
            except Exception as e:
                logger.debug(f"[On-chain] Falló la consulta fresca al cerrar: {e}")
                price = None
            if price is not None and price > 0:
                return price
            logger.debug("[On-chain] No se pudo refrescar el precio al cerrar; "
                         "uso el último precio conocido.")
        return self.latest_price

    async def _get_initial_price(self) -> Optional[float]:
        """Punto de entrada para conseguir el precio de compra real.

        1. Consigue un precio de REFERENCIA con la lógica de siempre
           (_get_reference_price): primer trade del feed en vivo, o
           fallback on-chain si el mint ya migró.
        2. Si `entry_dip_pct` es 0 (default), esa referencia ES el precio
           de entrada -comportamiento idéntico al de antes, sin cambios.
        3. Si `entry_dip_pct` > 0, la referencia NO se usa para comprar:
           se calcula el precio objetivo (referencia * (1 - dip%)) y se
           sigue mirando el precio (_wait_for_dip_entry) hasta que lo
           toque o baje de ahí, y ESE es el precio real de entrada.
        """
        reference_price = await self._get_reference_price()
        if reference_price is None:
            return None

        if self.cfg.entry_dip_pct <= 0:
            return reference_price

        target_price = reference_price * (1 - self.cfg.entry_dip_pct / 100.0)
        self._dip_target = target_price
        self._dip_reference = reference_price
        logger.info(f"Precio de referencia: {reference_price:.10f} SOL/token. Esperando una baja "
                    f"de {self.cfg.entry_dip_pct}% -> entra si el precio toca {target_price:.10f} "
                    f"SOL/token o menos (sin timeout, cancela con Ctrl+C si hace falta)...")
        return await self._wait_for_dip_entry(reference_price, target_price)

    async def _get_reference_price(self) -> Optional[float]:
        """Consigue el precio de REFERENCIA, en este orden:
          1. Feed en vivo de PumpPortal (subscribe_trade) — sin límite de
             tiempo MIENTRAS no haya llegado ni el ack de suscripción
             (eso indicaría un problema de conexión/api_key/wallet, no de
             mint migrado — ver diagnósticos más abajo).
          2. Una vez llega el ack, si no aparece NINGÚN trade real dentro
             de `live_feed_timeout_seconds`, asumimos que el mint ya
             migró a PumpSwap (subscribeTokenTrade no cubre esos casos,
             confirmado a mano) y probamos el fallback on-chain
             (OnchainFallback.resolve_reference).

        En PARALELO con lo anterior, desde el arranque, corre
        OnchainFallback.confirm_not_listed() en segundo plano (ver ese método):
        confirma cuanto antes -sin esperar ningún timeout pensado para
        tolerar tokens de poco volumen- si el mint directamente NO es de
        pump.fun/PumpSwap (ni bonding curve ni pool, ambas consultas
        confirmando ausencia). Es una pregunta que no depende de cuánto
        volumen tenga el token, así que no tiene sentido esperar a
        `live_feed_timeout_seconds` para hacerla.

        En cualquier momento de esta espera, Ctrl+C/SIGTERM corta todo de
        inmediato y devuelve None (todavía no hay posición abierta, así que no
        hay nada que vender).
        """
        logger.info("Esperando el primer trade en vivo del feed de PumpPortal (subscribe_trade) "
                    "para fijar el precio de entrada. Esto puede tardar si el token tiene poco volumen.")
        start = time.monotonic()
        last_reminder = start
        recibio_ack = False
        ack_received_at = 0.0  # solo cuenta con recibio_ack
        sin_precio = 0

        existence_check_task: Optional[asyncio.Task] = asyncio.ensure_future(
            self.onchain.confirm_not_listed()
        )
        try:
            while True:
                if existence_check_task is not None and existence_check_task.done():
                    mint_not_pumpfun = existence_check_task.result()
                    existence_check_task = None
                    if mint_not_pumpfun:
                        price = await self._jupiter_reference()
                        if price is not None:
                            return price
                        self._log_mint_not_pumpfun_abort()
                        self._initial_price_failure_reason_logged = True
                        return None

                timeout = None
                if recibio_ack:
                    elapsed_since_ack = time.monotonic() - ack_received_at
                    timeout = self.cfg.live_feed_timeout_seconds - elapsed_since_ack
                    if timeout <= 0:
                        if time.monotonic() - start >= self.cfg.entry_wait_timeout_seconds:
                            logger.error(f"[Feed en vivo] pasaron {self.cfg.entry_wait_timeout_seconds:.0f}s "
                                         f"en total esperando el precio de entrada, sin ningún trade real y "
                                         f"sin encontrar un pool de PumpSwap. Abortando esta entrada.")
                            self._initial_price_failure_reason_logged = True
                            return None

                        logger.debug(f"[Feed en vivo] pasaron {self.cfg.live_feed_timeout_seconds:.0f}s desde el "
                                     f"ack sin ningún trade real -> probablemente este mint ya migró a PumpSwap "
                                     f"y subscribeTokenTrade no lo cubre. Probando fallback on-chain...")
                        price, pool_confirmed_absent, mint_not_pumpfun = await self.onchain.resolve_reference()
                        if price is not None:
                            return price
                        if mint_not_pumpfun:
                            jupiter_price = await self._jupiter_reference()
                            if jupiter_price is not None:
                                return jupiter_price
                            self._log_mint_not_pumpfun_abort()
                            self._initial_price_failure_reason_logged = True
                            return None
                        if pool_confirmed_absent:
                            logger.info(f"[On-chain PumpSwap] Confirmado: todavía no hay pool de PumpSwap "
                                         f"para este mint -sigue en bonding curve, probablemente solo poco "
                                         f"volumen-. Sigo esperando el feed en vivo (hasta "
                                         f"{self.cfg.entry_wait_timeout_seconds:.0f}s en total)...")
                            ack_received_at = time.monotonic()  # reinicia la ventana antes del próximo intento
                            continue
                        logger.error("[On-chain PumpSwap] Tampoco se pudo obtener precio on-chain para este "
                                     "mint. No hay ninguna fuente de precio disponible; abortando esta entrada.")
                        self._initial_price_failure_reason_logged = True
                        return None

                    if existence_check_task is not None:
                        # Todavía no se resolvió la comprobación temprana de
                        # existencia: en vez de bloquearnos aquí hasta
                        # los live_feed_timeout_seconds completos,
                        # cortamos la espera en rebanadas cortas para
                        # poder revisarlo (arriba, al volver al inicio
                        # del loop) y abortar apenas confirme que el
                        # mint no es de pump.fun.
                        timeout = min(timeout, _EXISTENCE_CHECK_POLL_SECONDS)

                try:
                    event = await self._stream.next_event(timeout=timeout)
                except ShutdownRequested:
                    logger.info("Cancelado por el usuario mientras se esperaba el precio de entrada.")
                    return None
                except TimeoutError:
                    continue  # se recalcula el timeout restante y dispara el fallback arriba
                except StopAsyncIteration:
                    logger.warning("[Feed en vivo] la conexión se cerró antes de recibir un trade con precio.")
                    self._initial_price_failure_reason_logged = True
                    return None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[Feed en vivo] la conexión falló: {e}")
                    self._initial_price_failure_reason_logged = True
                    return None

                price = self.client.extract_price(event)
                if price is not None and price > 0:
                    logger.info(f"Precio de referencia (feed en vivo, subscribe_trade): {price:.10f} SOL/token")
                    return price

                # Distinguimos el ack de confirmación del subscribe (evento
                # con ÚNICAMENTE la clave "message", ej.
                # {"message": "Successfully subscribed to keys."}) de
                # cualquier otro evento con forma rara. El ack en sí es
                # normal y no indica ningún problema: solo confirma que la
                # suscripción fue aceptada.
                if not recibio_ack and set(event.keys()) == {"message"}:
                    recibio_ack = True
                    ack_received_at = time.monotonic()
                    logger.debug(f"[Feed en vivo] confirmación de suscripción recibida ({event['message']!r}). "
                                 f"Esperando hasta {self.cfg.live_feed_timeout_seconds:.0f}s más por un trade "
                                 f"real antes de recurrir al fallback on-chain...")
                else:
                    # Llegó un evento (que no es el ack) pero no se pudo
                    # calcular el precio. Lo avisamos, con las claves del
                    # evento, para poder diagnosticarlo sin quedar en silencio.
                    sin_precio += 1
                    if sin_precio == 1 or sin_precio % 20 == 0:
                        logger.debug(f"[Feed en vivo] llegaron eventos pero no se pudo calcular el precio "
                                     f"(claves del evento: {sorted(event.keys())}). Sigo esperando...")

                # Recordatorio periódico SOLO mientras no llegó ni el ack —
                # una vez que llega, el timeout de arriba ya se encarga de
                # decidir cuándo pasar al fallback, así que este recordatorio
                # sería redundante.
                if not recibio_ack:
                    now = time.monotonic()
                    if now - last_reminder >= _DIAGNOSTIC_REMINDER_SECONDS:
                        last_reminder = now
                        elapsed = now - start
                        logger.warning(f"[Feed en vivo] {elapsed:.0f}s esperando y todavía ni siquiera llegó "
                                       f"el ack de suscripción. Revisa la conexión de red y que el mint sea correcto.")
        finally:
            if existence_check_task is not None and not existence_check_task.done():
                existence_check_task.cancel()
                with contextlib.suppress(BaseException):
                    await existence_check_task

    async def _jupiter_reference(self) -> Optional[float]:
        """Mint que no está en ninguna fuente on-chain: si hay Jupiter, se
        intenta operar por ahí (ver OnchainFallback.resolve_jupiter). En
        REAL hace falta poder firmar; sin clave se avisa y no se entra."""
        jupiter = self.onchain.jupiter
        if jupiter is None:
            return None
        if self.cfg.live and not jupiter.can_sign:
            logger.error("Este token solo se puede operar por Jupiter, y para hacerlo en REAL falta la "
                         "clave privada de la wallet (wallet_private_key en [jupiter] del .toml).")
            return None
        logger.info("No cotiza contra SOL en pump.fun, PumpSwap, Raydium CPMM ni Meteora DLMM: "
                    "pruebo con Jupiter...")
        return await self.onchain.resolve_jupiter(self.cfg.buy_sol)

    def _log_mint_not_pumpfun_abort(self) -> None:
        """Único mensaje de error para el caso "este mint no es de
        pump.fun/PumpSwap" -antes se logueaba una vez en
        OnchainFallback.resolve_reference (o en la comprobación temprana) Y OTRA VEZ aquí,
        con textos parecidos pero no idénticos, lo que se leía como dos
        errores mezclados para un solo problema."""
        logger.error(f"Abortando esta entrada: no hay de dónde sacar el precio de {self.mint} "
                     f"(no tiene bonding curve de pump.fun, ni pool oficial de migración en "
                     f"PumpSwap, ni pool de Raydium CPMM ni de Meteora DLMM/DAMM v2 contra SOL con "
                     f"liquidez real). Si está en otro DEX (Meteora DAMM v1, Raydium AMM v4/CLMM, LaunchLab sin "
                     f"graduar...), este bot "
                     f"no puede calcular su precio de entrada"
                     + (" (tampoco se pudo por Jupiter, ver arriba)." if self.onchain.jupiter is not None else
                        ". Con una API key de Jupiter (jupiter_api_key) se podría operar por ahí."))

    async def _wait_for_dip_entry(self, reference_price: float, target_price: float) -> Optional[float]:
        """Sólo se llama cuando `entry_dip_pct` > 0 (ver _get_initial_price).

        Ya tenemos un precio de REFERENCIA (recién resuelto por
        _get_reference_price) pero todavía NO compramos con él. Aquí
        seguimos mirando el precio -por el mismo canal que produjo esa
        referencia: el feed en vivo ya suscripto, o polling on-chain si
        se resolvió por algún fallback (self.onchain.source)- hasta que
        toque `target_price` o baje de ahí, y ESE es el precio real de
        compra.

        Cada `status_interval_seconds` (mismo intervalo que usa el
        status printer una vez armada la posición) loguea el progreso:
        precio actual, objetivo, y cuánto falta bajar -para no quedar en
        silencio mientras se espera, sea porque el intervalo se cumplió
        aunque no haya llegado ningún trade nuevo (feed en vivo) o
        porque simplemente le toca su ciclo (polling on-chain).

        No hay timeout para la espera en sí: si el precio nunca baja lo
        suficiente, esto espera para siempre (igual que
        _get_reference_price esperando el ack). Ctrl+C/SIGTERM corta la
        espera en cualquier momento y devuelve None -todavía no hay
        posición abierta, no hay nada que vender.

        Actualiza self.latest_price en el camino (aunque todavía no haya
        posición, así el status printer/lo que consulte ese campo no se
        queda con el valor viejo de la referencia)."""
        self.latest_price = reference_price
        last_log = time.monotonic()

        if self.onchain.source is not None:
            while True:
                if self._shutdown_requested.is_set():
                    logger.info("Cancelado por el usuario mientras se esperaba la baja de entrada.")
                    return None
                try:
                    price = await self.onchain.fetch_price()
                except Exception as e:
                    logger.warning(f"[On-chain] error puntual esperando la baja de entrada, "
                                   f"reintento en el próximo ciclo: {e}")
                    price = None
                if price is not None and price > 0:
                    self.latest_price = price
                    if price <= target_price:
                        logger.info(f"Precio de entrada (baja de {self.cfg.entry_dip_pct}% desde "
                                    f"{reference_price:.10f}, fallback on-chain): {price:.10f} SOL/token")
                        return price
                    if time.monotonic() - last_log >= self.cfg.status_interval_seconds:
                        last_log = time.monotonic()
                        self._log_dip_wait_progress(target_price)
                try:
                    await asyncio.wait_for(
                        self._shutdown_requested.wait(),
                        timeout=self.cfg.onchain_poll_interval_seconds,
                    )
                    logger.info("Cancelado por el usuario mientras se esperaba la baja de entrada.")
                    return None
                except TimeoutError:
                    continue  # se cumplió el intervalo de polling sin pedido de shutdown; seguimos

        # Feed en vivo: reusamos la misma conexión/suscripción ya abierta.
        # Si se corta, reconectamos igual que hace _consume_trade_stream,
        # porque todavía no hay posición abierta que ese loop pueda cubrir.
        #
        # OJO: el log periódico de progreso corre en una tarea de fondo
        # aparte (_dip_progress_logger), NO metiendo un timeout en
        # TradeStream.next_event() para "despertarnos" cada tanto. Meterle un
        # timeout ahí cancelaría el __anext__() del generador
        # iter_trade_events en pleno vuelo -y cancelar un async generator
        # a mitad de un await lo deja CERRADO para siempre a nivel de
        # Python (no es que se corte la conexión real: el propio
        # generador queda inutilizable aunque el websocket siga
        # perfectamente abierto), lo que disparaba una reconexión real
        # innecesaria cada `status_interval_seconds`. Con la tarea de
        # fondo (que solo lee self.latest_price, igual que
        # _status_printer_loop) evitamos tocar el stream de eventos.
        progress_task = asyncio.create_task(self._dip_progress_logger(target_price))
        try:
            while True:
                try:
                    event = await self._stream.next_event()
                except ShutdownRequested:
                    logger.info("Cancelado por el usuario mientras se esperaba la baja de entrada.")
                    return None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # StopAsyncIteration (cierre "limpio" del server) ya
                    # entra por aquí: es subclase de Exception. Solo se
                    # distingue para el texto del log.
                    is_clean_close = isinstance(e, StopAsyncIteration)
                    motivo = "la conexión se cerró" if is_clean_close else f"conexión interrumpida ({e})"
                    logger.warning(f"[Feed en vivo] {motivo} mientras se esperaba la baja de entrada; "
                                   f"reconectando...")
                    try:
                        await self._stream.reconnect()
                        logger.info("Reconectado al feed de trades de PumpPortal.")
                        continue
                    except Exception as e2:
                        logger.error(f"No se pudo reconectar al feed de trades de PumpPortal mientras se "
                                     f"esperaba la baja de entrada: {e2}")
                        return None

                price = self.client.extract_price(event)
                if price is None or price <= 0:
                    continue
                self.latest_price = price
                if price <= target_price:
                    logger.info(f"Precio de entrada (baja de {self.cfg.entry_dip_pct}% desde "
                                f"{reference_price:.10f}, feed en vivo): {price:.10f} SOL/token")
                    return price
        finally:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass

    async def _dip_progress_logger(self, target_price: float) -> None:
        """Tarea de fondo (ver _wait_for_dip_entry, rama feed en vivo):
        cada `status_interval_seconds` loguea cuánto falta para llegar
        al precio de entrada, leyendo self.latest_price -sin tocar para
        nada el stream de eventos ni el generador que lo entrega (ver el
        comentario en _wait_for_dip_entry sobre por qué eso es
        importante). Se cancela desde _wait_for_dip_entry apenas termina
        de esperar, sea porque compró, la cancelaron, o falló."""
        while True:
            await asyncio.sleep(self._status_refresh_seconds())
            self._log_dip_wait_progress(target_price)

    def _log_dip_wait_progress(self, target_price: float) -> None:
        """Log periódico (ver _wait_for_dip_entry) de cuánto falta para
        llegar al precio de entrada: precio actual, objetivo, y el %%
        que todavía falta bajar DESDE el precio actual (no desde la
        referencia original) para tocar el objetivo."""
        if self.latest_price is None or self.latest_price <= 0:
            return
        falta_pct = (self.latest_price - target_price) / self.latest_price * 100.0
        if falta_pct <= 0:
            # No debería pasar (ya se habría disparado la compra), pero
            # por las dudas no mostramos un "falta bajar" negativo.
            return
        self._show_status(f"⏳ Esperando la baja de entrada | precio actual {self.latest_price:.10f} "
                          f"| objetivo {target_price:.10f} | falta bajar {falta_pct:.2f}% más")

    async def follow_after_close(self, seconds: float) -> None:
        """Con la posición YA vendida, sigue registrando el precio
        `seconds` más (gráfico e historial en la base de datos), para poder
        analizar después qué habría pasado con otros ajustes de trailing o
        stop. No toca la posición ni el estado del bot.

        Lee el precio on-chain cada `onchain_poll_interval_seconds` en vez
        de seguir suscrito al feed de PumpPortal: el feed se paga por
        evento, y un token activo puede mandar miles de trades en ese rato.
        Lo lanza el gestor en una tarea aparte (ver PositionManager), así
        que el mint queda libre aunque esto siga corriendo. Nunca lanza."""
        ctx = current_mint.set(self.mint)
        # Copia propia: no cambia la fuente que muestra el snapshot. Si el
        # precio venía del feed, el mint estaba en la bonding curve, y
        # fetch_price ya pasa solo a PumpSwap si la curva completa.
        onchain = OnchainFallback(self.mint, self.cfg.solana_rpc_url, self.onchain.source or "bondingcurve",
                                  jupiter=self.onchain.jupiter)
        interval = self.cfg.onchain_poll_interval_seconds
        if onchain.source == "jupiter":
            # Cada consulta gasta cupo de Jupiter, que comparten las
            # posiciones abiertas: más espaciado y siempre detrás de ellas.
            onchain.jupiter_tokens = self.onchain.jupiter_tokens
            onchain.jupiter_priority = PRIORITY_BACKGROUND
            interval = max(interval, _FOLLOW_JUPITER_INTERVAL_SECONDS)
        deadline = time.monotonic() + seconds
        logger.info(f"Registrando el precio {seconds / 60:.0f} min más tras la venta (para análisis).")
        try:
            while (left := deadline - time.monotonic()) > 0:
                try:
                    price = await onchain.fetch_price()
                except Exception as e:
                    logger.debug(f"[Tras la venta] no se pudo leer el precio on-chain: {e}")
                    price = None
                if price is not None and price > 0:
                    self._record_price(time.time(), price)
                await asyncio.sleep(min(interval, left))
            logger.debug("[Tras la venta] fin del registro de precios.")
        finally:
            current_mint.reset(ctx)

    async def _poll_onchain_price_loop(self) -> None:
        """Reemplazo de _consume_trade_stream para cuando la posición se
        abrió (o se detectó una migración a mitad de posición, ver
        _handle_feed_stall) vía algún fallback on-chain: como
        subscribeTokenTrade no está entregando nada útil para este
        mint, no hay forma de enterarse de nuevos precios por el feed en
        vivo -así que se consulta on-chain cada
        `onchain_poll_interval_seconds` mientras la posición siga
        abierta (bonding curve o PumpSwap, según `self.onchain.source`
        -ver OnchainFallback.fetch_price, que también se encarga de pasar de
        "bondingcurve" a "pumpswap" solo si la curva completa a mitad
        de este loop), y se alimenta al mismo _on_price_update() que
        usaría el feed en vivo (misma lógica de trailing-stop/stop-loss,
        solo cambia de dónde sale el precio).

        Un error puntual de RPC (timeout, rate limit, etc.) NO debe matar
        este loop para siempre -eso dejaría el precio congelado igual que
        el bug que tenía _consume_trade_stream-, así que cada iteración
        atrapa sus propios errores y sigue reintentando en el próximo
        ciclo."""
        while self.position is not None and not self.position.closed:
            try:
                price = await self.onchain.fetch_price()
                if price is not None and price > 0:
                    self.latest_price = price
                    await self._on_price_update(price)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[Polling on-chain] error puntual, reintento en el "
                               f"próximo ciclo: {e}")
            await asyncio.sleep(self.cfg.onchain_poll_interval_seconds)

    async def _consume_trade_stream(self) -> None:
        """Sigue escuchando trades en tiempo real por la conexión
        websocket ya abierta y suscripta desde run() (subscribe_trade).

        Si la conexión se cae (PumpPortal cierra el socket sin avisar,
        blip de red, etc.), se reconecta y se vuelve a suscribir
        automáticamente, con backoff exponencial, MIENTRAS la posición
        siga abierta. Sin esto, una caída de conexión dejaba el precio
        congelado para siempre: el trailing-stop/stop-loss quedaban
        ciegos y la única forma de salir era cerrar la posición a mano
        -exactamente lo que pasó.

        BUGFIX (migración a mitad de posición): una caída de CONEXIÓN no
        es el único caso que dejaba el precio congelado. Si el mint
        migra de la bonding curve de pump.fun a PumpSwap DESPUÉS de
        haber comprado (con el feed en vivo funcionando bien en el
        momento de la entrada), subscribeTokenTrade simplemente deja de
        mandar trades para ese mint de forma silenciosa: el socket sigue
        abierto, no hay error ni cierre, así que ni el `except Exception`
        de aquí abajo ni el StopAsyncIteration se enteraban -el precio
        quedaba pegado en el último valor para siempre y el status
        printer lo repetía sin parar, como si nada (exactamente el
        síntoma reportado: precio congelado en 0.0000032441 sin ningún
        aviso de "conexión interrumpida"). Por eso ahora cada espera de
        trade tiene un timeout (`stall_timeout_seconds`); si se cumple,
        _handle_feed_stall() confirma con una consulta on-chain puntual
        -igual que se hace para el precio de ENTRADA en
        _get_reference_price- y, si hay un pool de PumpSwap con precio
        válido, pasa a polling on-chain para el resto de la posición en
        vez de seguir esperando trades que ya no van a llegar."""
        backoff = 2.0
        max_backoff = 30.0
        while self.position is not None and not self.position.closed:
            try:
                while True:
                    if self.position is None or self.position.closed:
                        return
                    try:
                        event = await self._stream.next_event(timeout=self.cfg.stall_timeout_seconds)
                    except TimeoutError:
                        if await self._handle_feed_stall():
                            return  # migró: _handle_feed_stall ya corrió el polling on-chain hasta el cierre
                        continue  # solo poco volumen: seguimos esperando el feed en vivo
                    price = self.client.extract_price(event)
                    if price is None or price <= 0:
                        continue
                    self.latest_price = price
                    await self._on_price_update(price)
                    backoff = 2.0  # se recibió un evento bueno: reseteamos el backoff
                    # El feed volvió a entregar: los stalls acumulados ya
                    # no cuentan como "el feed está muerto para este mint".
                    self._curve_stalls = 0
                # (inalcanzable: el while True interno solo se sale por return)
            except ShutdownRequested:
                return
            except StopAsyncIteration:
                # La conexión se cerró de forma "limpia" del lado del server.
                if self.position is None or self.position.closed:
                    return
                logger.warning("[Feed de trades de PumpPortal] la conexión se cerró.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[Feed de trades de PumpPortal] conexión interrumpida: {e}")

            if self.position is None or self.position.closed:
                return

            logger.warning(f"Reconectando al feed de trades de PumpPortal en {backoff:.0f}s "
                           f"(posición sigue abierta, no puedo perder el precio)...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

            try:
                await self._stream.reconnect()
                logger.info("Reconectado al feed de trades de PumpPortal.")
            except Exception as e:
                logger.warning(f"No se pudo reconectar todavía: {e}. Reintento en {backoff:.0f}s...")

    async def _handle_feed_stall(self) -> bool:
        """Se llama cuando pasaron `stall_timeout_seconds` sin ningún
        trade nuevo del feed en vivo, con la posición ya abierta.
        Confirma con consultas on-chain puntuales qué está pasando:

          1. Primero comprueba si hay un pool de PumpSwap con precio
             válido -es una migración real (no solo una pausa de
             volumen). Si lo hay: aplica ese precio de inmediato, marca
             `onchain.source = "pumpswap"` y corre el polling on-chain
             (_poll_onchain_price_loop) hasta que la posición se cierre
             -> devuelve True (el llamador debe dejar de esperar el
             feed en vivo, que ya sabemos que no va a entregar nada más
             para este mint).

          2. Si NO hay pool de PumpSwap, el mint sigue en bonding curve
             -pero eso no explica por qué subscribeTokenTrade dejó de
             mandar trades (el síntoma reportado: puede pasar aunque el
             mint siga perfectamente activo en la curva). En vez de
             asumir sin más "poco volumen" y quedarnos ciegos hasta el
             próximo trade real, leemos el precio DIRECTO de la cuenta
             de la bonding curve on-chain. Si responde con un precio
             válido, lo aplicamos como red de seguridad (sin abandonar
             el feed en vivo: seguimos reintentando reconectar/recibir
             trades reales en el loop de _consume_trade_stream) ->
             devuelve False de todos modos, pero con self.latest_price ya
             refrescado en vez de congelado.

          3. Si ni el pool ni la bonding curve dan nada legible ->
             probablemente es solo un token con muy poco volumen
             momentáneo -> devuelve False sin tocar el precio."""
        if self.position is None or self.position.closed:
            return True
        price = await self.onchain.migrated_price()
        if price is not None and price > 0:
            logger.warning(f"[Feed de trades de PumpPortal] sin trades nuevos hace "
                           f"{self.cfg.stall_timeout_seconds:.0f}s y se confirmó un pool de PumpSwap "
                           f"con precio válido -> el mint migró a mitad de la posición "
                           f"(subscribeTokenTrade no lo va a cubrir más). Paso a polling on-chain cada "
                           f"{self.cfg.onchain_poll_interval_seconds:.0f}s para no perder el precio.")
            self.onchain.source = "pumpswap"
            self.latest_price = price
            await self._on_price_update(price)
            if self.position is None or self.position.closed:
                return True
            await self._poll_onchain_price_loop()
            return True

        curve_price, complete, _exists = await self.onchain.curve_status()
        if curve_price is not None and not complete:
            self._curve_stalls += 1
            if self._curve_stalls >= _MAX_CURVE_STALLS_BEFORE_POLLING:
                # El feed lleva varios ciclos sin entregar NADA para un
                # mint que sigue perfectamente vivo en la curva. Seguir
                # esperándolo significa evaluar el trailing-stop una vez
                # cada stall_timeout_seconds (10s por defecto): demasiado
                # lento para una memecoin. Pasamos a polling on-chain de
                # la bonding curve, que es la MISMA fuente contra la que
                # se ejecuta el trade, cada onchain_poll_interval_seconds.
                logger.warning(f"[Feed de trades de PumpPortal] {self._curve_stalls} ciclos seguidos "
                               f"sin trades ({self.cfg.stall_timeout_seconds:.0f}s cada uno) con la "
                               f"bonding curve todavía activa -> dejo de depender del feed en vivo "
                               f"para esta posición y paso a leer el precio de la curva on-chain cada "
                               f"{self.cfg.onchain_poll_interval_seconds:.0f}s.")
                self.onchain.source = "bondingcurve"
                self.latest_price = curve_price
                await self._on_price_update(curve_price)
                if self.position is None or self.position.closed:
                    return True
                await self._poll_onchain_price_loop()
                return True

            logger.info(f"[Feed de trades de PumpPortal] sin trades nuevos hace "
                        f"{self.cfg.stall_timeout_seconds:.0f}s, pero la bonding curve sigue activa "
                        f"con precio válido on-chain ({curve_price:.10f} SOL/token) -> lo uso como "
                        f"red de seguridad mientras sigo intentando recibir trades reales del feed en "
                        f"vivo.")
            self.latest_price = curve_price
            await self._on_price_update(curve_price)
            return False

        logger.debug(f"[Feed de trades de PumpPortal] sin trades nuevos hace "
                     f"{self.cfg.stall_timeout_seconds:.0f}s, y tampoco se pudo leer un precio válido "
                     f"ni de un pool de PumpSwap ni de la bonding curve -> probablemente solo poco "
                     f"volumen, sigo esperando el feed en vivo.")
        return False

    async def _load_token_name(self) -> None:
        found = await self._metadata_client.fetch_name_symbol(self.mint)
        if not found:
            return
        self.token_name, self.token_symbol = found
        if self.token_symbol:
            set_mint_label(self.mint, f"${self.token_symbol} {self.mint[:4]}")
        self._persist()
        logger.info(f"Token: {self._token_label()}")
        self._update_panel_fixed()

    def _token_label(self) -> str:
        if self.token_symbol and self.token_name and self.token_symbol != self.token_name:
            return f"{self.token_name} (${self.token_symbol})"
        return self.token_name or f"${self.token_symbol}"

    def snapshot(self) -> dict:
        """Foto del estado actual del bot, para mostrarla desde fuera (la
        web, más adelante). Solo tipos simples (str/float/bool/None/dict),
        así se puede pasar tal cual a JSON. No cambia nada: se puede
        llamar en cualquier momento y desde cualquier tarea del loop."""
        cfg = self.cfg
        pos = self.position
        price = self.latest_price
        phase = "closed" if pos is not None and pos.closed else self.phase
        snap = {
            "mint": self.mint,
            "token_name": self.token_name,
            "token_symbol": self.token_symbol,
            "live": cfg.live,
            "phase": phase,
            "running": self.running,
            "close_requested": self._shutdown_requested.is_set(),
            "recovered": self.recovered,
            "detached": self.detached,
            "close_reason": self._close_reason,
            "end_reason": self.end_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latest_price": price,
            # "feed" = subscribeTokenTrade en vivo; si no, qué fallback
            # on-chain está dando el precio (ver OnchainFallback.source).
            "price_source": self.onchain.source or "feed",
            # Por dónde irán (o fueron) las órdenes de esta posición.
            "order_route": "jupiter" if self.onchain.pool_override() == JUPITER_POOL else "pumpportal",
            "entry_target_price": self._dip_target,
            "sell_failures": self.sell_failures,
            "entry_reference_price": self._dip_reference,
            "settings": {
                "buy_sol": cfg.buy_sol,
                "slippage": cfg.slippage,
                "priority_fee": cfg.priority_fee,
                "pool": cfg.pool,
                "entry_dip_pct": cfg.entry_dip_pct,
                "activation_pct": cfg.activation_pct,
                "trailing_pct": cfg.trailing_pct,
                "initial_stop_pct": cfg.initial_stop_pct,
                "strategy_price_basis": cfg.strategy_price_basis,
            },
            "position": None,
        }
        if pos is None:
            return snap

        strategy = self.strategy
        live_price = price is not None and price > 0 and not pos.closed
        snap["position"] = {
            "entry_price": pos.entry_price,
            "market_entry_price": pos.market_entry_price,
            "entry_is_real_fill": pos.entry_is_real_fill,
            "has_entry_costs": pos.has_entry_costs(),
            "sol_amount": pos.sol_amount,
            "token_amount": pos.token_amount,
            "opened_at": pos.opened_at,
            "armed": pos.armed,
            "armed_at": pos.armed_at,
            "highest_price": pos.highest_price,
            "activation_price": strategy.activation_price(pos),
            "initial_stop_price": strategy.initial_stop_price(pos),
            "trailing_stop_price": strategy.trailing_stop_price(pos),
            "market_pnl_pct": pos.market_pnl_pct(price) if live_price and price is not None else None,
            "net_pnl_pct": pos.pnl_pct(price) if live_price and price is not None else None,
            "closed": pos.closed,
            "closed_at": pos.closed_at,
            "exit_price": pos.exit_price,
            "proceeds_sol": pos.proceeds_sol,
            "realized_pnl_sol": pos.realized_pnl_sol,
            "realized_pnl_pct": pos.realized_pnl_pct,
            "close_reason": pos.close_reason,
            "exit_is_real_fill": pos.exit_is_real_fill,
        }
        return snap

    def _update_panel_fixed(self) -> None:
        """Bloque fijo del panel: todo lo que no cambia mientras corre el
        bot (y, una vez comprado, los niveles calculados desde la entrada)."""
        if self.panel is None:
            return
        cfg = self.cfg
        modo = "REAL" if cfg.live else "SIMULADO"
        priority_fee = f"{cfg.priority_fee:.9f}".rstrip("0").rstrip(".")
        token = f" | {self._token_label()}" if (self.token_name or self.token_symbol) else ""
        lines = [
            f" PePump | Modo {modo}{token} | Mint {self.mint}",
            f" Compra {cfg.buy_sol} SOL | slippage {cfg.slippage}% | priority fee {priority_fee} SOL "
            f"| pool {cfg.pool}" + (f" | baja de entrada -{cfg.entry_dip_pct}%" if cfg.entry_dip_pct > 0 else ""),
        ]
        pos = self.position
        if pos is None:
            lines.append(f" Activación +{cfg.activation_pct}% | trailing {cfg.trailing_pct}% "
                         f"| stop-loss inicial -{cfg.initial_stop_pct}%")
        else:
            coste = f" | {pos.cost_label()} {pos.entry_price:.10f}" if pos.has_entry_costs() else ""
            lines.append(f" Entrada {pos.market_entry_price:.10f} SOL/token{coste}")
            strategy = self.strategy
            base = "precio real" if cfg.strategy_price_basis == "real" else "precio de mercado"
            lines.append(f" Activación +{cfg.activation_pct}% (>= {strategy.activation_price(pos):.10f}) "
                         f"| trailing {cfg.trailing_pct}% "
                         f"| stop-loss inicial -{cfg.initial_stop_pct}% "
                         f"(<= {strategy.initial_stop_price(pos):.10f}) | sobre {base}")
        self.panel.set_fixed(lines)

    def _show_status(self, line: str) -> None:
        """Línea de estado periódica: con panel se reescribe en su sitio,
        sin panel se loguea como siempre."""
        if self.panel is not None:
            self.panel.set_status(line)
        else:
            logger.info(line)

    def _status_refresh_seconds(self) -> float:
        # Con panel, reescribir no ensucia la pantalla, así que se refresca
        # más seguido que el intervalo de log (que sigue valiendo sin panel).
        if self.panel is not None:
            return min(self.status_interval_seconds, 1.0)
        return self.status_interval_seconds

    async def _status_printer_loop(self) -> None:
        """Imprime el %% de profit actual cada `status_interval_seconds`, sin
        depender de que lleguen nuevas operaciones del token en ese momento.
        Este es el ÚNICO lugar que imprime el estado de forma periódica."""
        while True:
            await asyncio.sleep(self._status_refresh_seconds())
            if self.position is None or self.position.closed or self.latest_price is None:
                continue
            pos = self.position
            # DOS porcentajes a propósito, porque miden cosas distintas y
            # confundirlos hace parecer que el bot calcula mal:
            #   - "mercado": cuánto se movió el precio desde la compra. Es
            #     el número que muestra pump.fun y el que usan los
            #     umbrales de la estrategia.
            #   - "neto": lo que realmente te llevarías vendiendo ahora,
            #     contando lo que costó entrar (comisiones de pump.fun y
            #     PumpPortal, priority fee y rent de la cuenta de token).
            # La brecha entre los dos es fija: pos.entry_cost_pct().
            pnl = pos.market_pnl_pct(self.latest_price)
            neto = (f" | neto {pos.pnl_pct(self.latest_price):+.2f}%"
                    if pos.has_entry_costs() else "")
            if self.sell_failures:
                neto += f" | ⚠ {self.sell_failures} ventas fallidas, reintentando"

            if pos.armed:
                stop_price = self.strategy.trailing_stop_price(pos)
                self._show_status(f"⏱️  [armado] precio {self.latest_price:.10f} | máximo {pos.highest_price:.10f} "
                                  f"| nivel de venta {stop_price:.10f} | mercado: {pnl:+.2f}%{neto}")
            else:
                # `entrada` es el precio de MERCADO contra el que se mide
                # la activación; el precio EFECTIVO pagado (con
                # comisiones) se muestra aparte para que no parezca un
                # error ver el neto en negativo con el precio clavado en
                # la entrada.
                coste = (f" | {pos.cost_label()} {pos.entry_price:.10f}" if pos.has_entry_costs() else "")
                self._show_status(f"⏱️  [esperando activación +{self.cfg.activation_pct}%] "
                                  f"precio {self.latest_price:.10f} | entrada {pos.market_entry_price:.10f}"
                                  f"{coste} | mercado: {pnl:+.2f}%{neto}")

    async def _on_first_price(self, price: float) -> None:
        self.position = await self.executor.buy(self.mint, price, pool_override=self.onchain.pool_override())
        self.onchain.jupiter_tokens = self.position.token_amount
        self._update_panel_fixed()
        logger.info(f"Activación del trailing-stop: +{self.cfg.activation_pct}% "
                    f"| ancho del trailing una vez armado: {self.cfg.trailing_pct}% "
                    f"| stop-loss inicial (antes de armar): -{self.cfg.initial_stop_pct}%")

    async def _on_price_update(self, price: float) -> None:
        """Aplica la decisión de la estrategia (ver strategy.py) a la
        posición: armar, registrar un máximo nuevo o vender."""
        pos = self.position
        if pos is None or pos.closed:
            return
        strategy = self.strategy
        decision = strategy.evaluate(pos, price)

        if decision.action is Action.ARM:
            pos.armed = True
            pos.armed_at = time.time()
            pos.highest_price = price
            self._persist()
            logger.info(f"✅ Trailing-stop ARMADO. Precio actual {price:.10f} "
                        f"(PnL {strategy.pnl_pct(pos, price):+.2f}%). Máximo inicial registrado.")
        elif decision.action is Action.NEW_HIGH:
            pos.highest_price = price
            self._persist()
            logger.info(f"📈 Nuevo máximo: {price:.10f} (PnL {strategy.pnl_pct(pos, price):+.2f}%) "
                        f"| nuevo nivel de venta (trailing): {strategy.trailing_stop_price(pos):.10f}")
        elif decision.is_sell:
            await self._try_sell(pos, price, decision.reason)

    async def _try_sell(self, pos: Position, price: float, reason: str) -> None:
        """Envuelve executor.sell(): si la venta REAL falla (Lightning API
        devuelve error, o la tx confirma pero FALLA on-chain -p. ej. por
        slippage excedido-), executor.sell() ahora propaga la excepción a
        propósito en vez de marcar la posición como cerrada (ver BUGFIX en
        executor.py). Aquí la atajamos para que ese fallo:
          - se loguee como lo que es (venta fallida), no como "conexión
            interrumpida" (que es lo que pasaría si se colara hasta el
            except genérico de _consume_trade_stream), y
          - NO trabe el bot para siempre: como la posición sigue abierta
            (closed=False) y NO seteamos _closed_event, el próximo precio
            que llegue (pasada la pausa entre reintentos) vuelve a evaluar
            la condición de salida y reintenta la venta sola, con más
            slippage y priority fee (ver _sell)."""
        async with self._sell_lock:
            # Revalidamos DESPUÉS de conseguir el lock: si _sell_on_shutdown
            # (u otra llamada) ya vendió mientras esperábamos aquí, esto ya
            # no corresponde -evita el segundo pedido real a la Lightning API.
            if pos.closed:
                return
            # Todavía en la pausa tras una venta fallida: el siguiente
            # precio que llegue después lo vuelve a intentar.
            if time.monotonic() < self._sell_retry_at:
                return
            if not await self._sell(pos, price, reason):
                return
        self._persist()
        self._closed_event.set()

    async def _sell(self, pos: Position, price: float, reason: str) -> bool:
        """Un intento de venta, con el slippage y el priority fee que tocan
        según las ventas fallidas anteriores (ver sell_retry_params). True
        si se vendió. Si falla, cuenta el fallo y fija cuándo se puede
        volver a intentar. Se llama siempre con _sell_lock tomado."""
        slippage, priority_fee = sell_retry_params(self.cfg, self.sell_failures)
        try:
            await self.executor.sell(pos, price, reason, pool_override=self.onchain.pool_override(),
                                     slippage=slippage, priority_fee=priority_fee)
        except Exception as e:
            self.sell_failures += 1
            # Pausa corta y creciente (1, 2, ... hasta 5 s): no machacar la
            # Lightning API con la misma orden, pero sin dejar el stop
            # mucho tiempo sin ejecutar.
            self._sell_retry_at = time.monotonic() + min(self.sell_failures, 5) * self._SELL_RETRY_DELAY_SECONDS
            next_slippage, next_fee = sell_retry_params(self.cfg, self.sell_failures)
            logger.warning(f"⚠️  Venta fallida ({reason}), intento {self.sell_failures}: {e}. La posición "
                           f"SIGUE ABIERTA; el próximo intento va con slippage {next_slippage:g}% y "
                           f"priority fee {next_fee:.6f} SOL.")
            if self.sell_failures == _SELL_FAILURES_ALERT:
                logger.error(f"🚨 {self.sell_failures} ventas fallidas seguidas de {self.mint}. El bot sigue "
                             f"reintentando, pero revisa la posición (saldo de SOL para fees, RPC, "
                             f"liquidez): https://pump.fun/{self.mint}")
            return False
        self.sell_failures = 0
        return True
