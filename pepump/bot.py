import asyncio
import logging
import signal
import time
from typing import AsyncIterator, Optional

from pepump.executor import Position
from pepump.pump import PumpSwapOnChainClient

logger = logging.getLogger(__name__)

# Cada cuánto tiempo (segundos), mientras seguimos esperando el primer
# trade real, se repite el recordatorio de diagnóstico con las causas
# más probables (wallet sin fondos, api_key inválida, token sin volumen).
_DIAGNOSTIC_REMINDER_SECONDS = 10.0


class _ShutdownRequested(Exception):
    """Señal interna: se pidió apagado (Ctrl+C/SIGTERM) mientras se
    esperaba otra cosa (un evento del feed, el cierre de la posición).
    Nunca se propaga fuera de este módulo."""


class TrailingTakeProfitBot:
    """
    Orquesta todo: escucha el feed de precios vía PumpPortalClient y decide,
    a través de un TradeExecutor, cuándo comprar y cuándo vender según la
    lógica de trailing take-profit / stop-loss inicial.
    """

    def __init__(self, client, executor, config):
        self.client = client
        self.executor = executor
        self.mint = config.mint
        self.cfg = config
        self.status_interval_seconds = config.status_interval_seconds
        self.position: Optional[Position] = None
        self.latest_price: Optional[float] = None
        self._closed_event = asyncio.Event()
        self._ws = None
        self._trade_events: Optional[AsyncIterator[dict]] = None
        # BUGFIX (generador cerrado por timeout): ver _next_trade_event.
        # Tarea pendiente de `self._trade_events.__anext__()` que puede
        # sobrevivir a un timeout sin cancelarse, para poder seguir
        # esperando el MISMO evento en la próxima llamada en vez de
        # cortar el generador async subyacente.
        self._pending_next_event_task: Optional[asyncio.Task] = None
        # True si tuvimos que resolver el precio de entrada por el
        # fallback on-chain de PumpSwap (ver _try_onchain_fallback) en
        # vez del feed en vivo de PumpPortal -> significa que el mint ya
        # migró y subscribeTokenTrade no entrega nada para él, así que el
        # monitoreo de la posición TAMBIÉN necesita el polling on-chain
        # (ver _poll_onchain_price_loop), no solo el precio inicial.
        self._using_onchain_fallback = False
        # Se activa con Ctrl+C (SIGINT) o SIGTERM (ver run()). NO se usa
        # el try/except KeyboardInterrupt clásico porque en asyncio esa
        # señal interrumpe el loop de eventos "por afuera" de la
        # corrutina en ejecución, no adentro de ella -no hay garantía de
        # que un try/except puesto en el código de la app la agarre. Con
        # loop.add_signal_handler() el apagado se coordina de forma
        # confiable con un asyncio.Event normal.
        self._shutdown_requested = asyncio.Event()
        # BUGFIX (doble venta): serializa CUALQUIER intento de venta de la
        # posición (ya sea por trailing-stop/stop-loss vía _try_sell, o por
        # cierre manual vía _sell_on_shutdown). No alcanza con solo
        # reordenar la cancelación de tareas en run() para evitar la
        # carrera: execute_lightning_trade manda el POST real dentro de un
        # asyncio.to_thread, y cancelar la tarea que está esperando ese
        # await NO mata el hilo -el pedido HTTP ya en vuelo puede seguir
        # llegando al server igual. Con este lock, si dos caminos intentan
        # vender casi al mismo tiempo, el segundo espera, ve `pos.closed`
        # ya en True (o el executor.sell tira porque no queda nada que
        # vender) y no dispara un segundo pedido real.
        self._sell_lock = asyncio.Lock()

    def _request_shutdown(self, sig_name: str) -> None:
        if self._shutdown_requested.is_set():
            # Segundo Ctrl+C mientras ya se está vendiendo/cerrando: no
            # hacemos nada especial acá (no forzamos un corte abrupto),
            # simplemente evitamos loguear el aviso de nuevo.
            return
        logger.warning(f"⚠️  {sig_name} recibido. Cerrando ordenadamente "
                       f"(si hay una posición abierta, se vende al precio actual)...")
        self._shutdown_requested.set()

    async def run(self) -> None:
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

        Ctrl+C (SIGINT) o SIGTERM en cualquier momento: si ya hay una
        posición abierta, se vende al precio más actual posible antes de
        salir (ver _sell_on_shutdown); si todavía no se compró nada,
        simplemente corta la espera y termina sin vender nada.
        """
        logger.info(f"Siguiendo el token: {self.mint}")
        logger.debug(f"Suscribiéndose (subscribe_trade) al feed de trades de PumpPortal para {self.mint}...")

        loop = asyncio.get_running_loop()
        signal_handlers_installed = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown, sig.name)
                signal_handlers_installed.append(sig)
            except (NotImplementedError, RuntimeError):
                # Windows (ProactorEventLoop) no soporta add_signal_handler.
                # Ctrl+C ahí cae al comportamiento default de Python
                # (KeyboardInterrupt sin venta automática al cerrar).
                logger.debug(f"No se pudo instalar manejador para {sig.name} en este sistema "
                             f"(¿Windows?); el cierre ordenado con venta automática no va a "
                             f"funcionar para esta señal.")

        tasks = []
        # OJO: connect_trade_stream ya deja el subscribe MANDADO del lado
        # de PumpPortal apenas conecta. Si algo revienta después de esto y
        # antes de que el finally pueda correr, la conexión queda
        # suscripta pero abandonada del lado del servidor (no se le avisa
        # con un cierre prolijo de WebSocket, solo se corta el TCP cuando
        # el proceso muere). Por eso TODO lo que dependa de self._ws vive
        # dentro de este try/finally, sin excepciones: así cualquier
        # crash -incluso uno inesperado que no previmos- cierra el socket
        # de forma ordenada en vez de dejarlo zombie.
        try:
            # BUGFIX: antes, si connect_trade_stream fallaba (red caída,
            # DNS, api_key rechazada al nivel de handshake, etc.), la
            # excepción se escapaba sin capturar hasta afuera de run() ->
            # run.py solo atrapa KeyboardInterrupt, así que el bot moría
            # con un traceback crudo en vez de un mensaje claro. Ahora se
            # loguea el error y se sale ordenadamente (todavía no hay
            # posición abierta, así que no hay nada que vender).
            try:
                self._ws = await self.client.connect_trade_stream(self.mint)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"No se pudo conectar/suscribir al feed de trades de PumpPortal "
                             f"({self.client.DATA_WS_URL}): {e}")
                return
            self._trade_events = self.client.iter_trade_events(self._ws)

            initial_price = await self._get_initial_price()
            if initial_price is None:
                if self._shutdown_requested.is_set():
                    logger.info("Cancelado antes de abrir posición; no hay nada que vender.")
                else:
                    logger.error("Se cortó la conexión con PumpPortal antes de recibir un trade con "
                                 "precio. Verificá la dirección del token y la api_key, y volvé a intentar.")
                return

            self.latest_price = initial_price
            try:
                await self._on_first_price(initial_price)
            except Exception as e:
                logger.error(f"La compra no se confirmó on-chain, no se abrió ninguna posición: {e}")
                return

            if self._using_onchain_fallback:
                # subscribeTokenTrade no entrega nada para este mint (ya
                # migrado) -> el monitoreo de la posición usa polling
                # on-chain en vez del consumidor del feed en vivo.
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
            # se cancelan acá ANTES de vender, así que cuando corre
            # _sell_on_shutdown ya no hay nada más que pueda pisarle la venta.
            need_shutdown_sell = await self._wait_for_close_or_shutdown()

            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            if need_shutdown_sell:
                if self.position is not None and not self.position.closed:
                    logger.warning("Vendiendo la posición abierta al precio actual antes de salir...")
                    await self._sell_on_shutdown()
                else:
                    logger.info("No hay posición abierta; no hay nada que vender.")
        finally:
            for sig in signal_handlers_installed:
                try:
                    loop.remove_signal_handler(sig)
                except Exception:
                    pass
            if self._pending_next_event_task is not None:
                self._pending_next_event_task.cancel()
                self._pending_next_event_task = None
            if self._ws is not None:
                await self._ws.close()

        logger.info("Bot finalizado.")

    async def _wait_for_close_or_shutdown(self) -> bool:
        """Espera a que la posición se cierre sola (TP/SL normal) O a que
        se pida un apagado (Ctrl+C/SIGTERM).

        A propósito NO vende acá adentro (ver BUGFIX en run()): solo
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
        try:
            await asyncio.wait(
                {closed_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for t in (closed_task, shutdown_task):
                if not t.done():
                    t.cancel()

        if self._closed_event.is_set():
            return False  # se cerró sola (TP/SL), no hace falta hacer nada más

        return self._shutdown_requested.is_set()

    async def _sell_on_shutdown(self) -> None:
        """Intenta conseguir el precio MÁS actual posible (una consulta
        on-chain fresca si veníamos usando ese fallback; si no, el último
        precio que ya venía actualizando el feed en vivo, que está
        prácticamente en tiempo real) y vende de una la posición
        abierta.

        Usa _sell_lock (ver __init__) para no pisarse con un _try_sell
        del monitor que haya quedado en vuelo. Revalida `position.closed`
        DESPUÉS de conseguir el lock: si _try_sell ya vendió mientras
        esperábamos acá, no hace falta (ni corresponde) vender de nuevo."""
        async with self._sell_lock:
            if self.position is None or self.position.closed:
                logger.info("No hay posición abierta; no hay nada que vender.")
                return
            price = await self._resolve_shutdown_price()
            if price is None or price <= 0:
                logger.error("No se pudo determinar ningún precio para vender al cerrar. La posición "
                             f"queda ABIERTA — revisala manualmente: https://pump.fun/{self.mint}")
                return
            try:
                await self.executor.sell(self.position, price, "cierre manual (Ctrl+C/SIGTERM)")
            except Exception as e:
                logger.error(f"Falló la venta de cierre manual: {e}. La posición SIGUE ABIERTA — "
                             f"revisala manualmente: https://pump.fun/{self.mint}")

    async def _resolve_shutdown_price(self) -> Optional[float]:
        if self._using_onchain_fallback:
            onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
            try:
                price = await onchain.fetch_price_for_migrated_mint(self.mint)
            except Exception as e:
                logger.debug(f"[On-chain PumpSwap] Falló la consulta fresca al cerrar: {e}")
                price = None
            if price is not None and price > 0:
                return price
            logger.debug("[On-chain PumpSwap] No se pudo refrescar el precio al cerrar; "
                         "uso el último precio conocido.")
        return self.latest_price

    async def _next_trade_event(self, timeout: Optional[float] = None) -> dict:
        """__anext__() de self._trade_events, pero compitiendo contra
        `_shutdown_requested` (y, si se pasa `timeout`, contra un
        deadline). Lanza _ShutdownRequested si gana el apagado,
        asyncio.TimeoutError si gana el timeout, o deja pasar cualquier
        excepción normal del feed (StopAsyncIteration, errores de red,
        etc.).

        BUGFIX: antes, cuando ganaba el timeout, se cancelaba
        directamente la tarea que envolvía `self._trade_events.__anext__()`.
        Cancelar esa tarea tira un CancelledError DENTRO del generador
        async en su punto de espera (ej. el `await websocket.recv()`
        interno de iter_trade_events) -y como nada lo atrapa ahí adentro,
        el generador queda CERRADO para siempre: cualquier __anext__()
        posterior sobre el mismo generador devuelve StopAsyncIteration
        de una, aunque la conexión siga perfectamente viva. Esto rompía
        en silencio cualquier código que esperara poder seguir
        escuchando el mismo feed después de un timeout (ver
        _get_reference_price reintentando tras confirmar 'sin pool', y
        _consume_trade_stream retomando el feed tras un stall sin
        migración real).

        Ahora, si gana el timeout, NO se cancela la tarea: se guarda en
        self._pending_next_event_task para reutilizarla en la próxima
        llamada -mismo generador, mismo __anext__() en vuelo, sin
        cortar nada-. Recién se cancela de verdad si gana el shutdown
        (ahí sí termina todo)."""
        if self._pending_next_event_task is None or self._pending_next_event_task.done():
            next_task = asyncio.ensure_future(self._trade_events.__anext__())
        else:
            next_task = self._pending_next_event_task
        shutdown_task = asyncio.ensure_future(self._shutdown_requested.wait())
        try:
            done, _pending = await asyncio.wait(
                {next_task, shutdown_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            pass

        if shutdown_task in done:
            next_task.cancel()
            self._pending_next_event_task = None
            raise _ShutdownRequested()

        shutdown_task.cancel()

        if next_task in done:
            self._pending_next_event_task = None
            return next_task.result()  # puede propagar StopAsyncIteration u otra excepción

        # Ganó el timeout: dejamos next_task VIVA (sin cancelar) para
        # retomarla en la próxima llamada en vez de cerrar el generador.
        self._pending_next_event_task = next_task
        raise asyncio.TimeoutError()

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
        logger.info(f"Precio de referencia: {reference_price:.10f} SOL/token. Esperando una baja "
                    f"de {self.cfg.entry_dip_pct}% -> entra si el precio toca {target_price:.10f} "
                    f"SOL/token o menos (sin timeout, cancelá con Ctrl+C si hace falta)...")
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
             (_try_onchain_fallback).

        En cualquier momento de esta espera, Ctrl+C/SIGTERM corta todo de
        una y devuelve None (todavía no hay posición abierta, así que no
        hay nada que vender).
        """
        logger.info("Esperando el primer trade en vivo del feed de PumpPortal (subscribe_trade) "
                    "para fijar el precio de entrada. Esto puede tardar si el token tiene poco volumen.")
        start = time.monotonic()
        last_reminder = start
        recibio_ack = False
        ack_received_at: Optional[float] = None
        sin_precio = 0

        while True:
            timeout = None
            if recibio_ack:
                elapsed_since_ack = time.monotonic() - ack_received_at
                timeout = self.cfg.live_feed_timeout_seconds - elapsed_since_ack
                if timeout <= 0:
                    if time.monotonic() - start >= self.cfg.entry_wait_timeout_seconds:
                        logger.error(f"[Feed en vivo] pasaron {self.cfg.entry_wait_timeout_seconds:.0f}s "
                                     f"en total esperando el precio de entrada, sin ningún trade real y "
                                     f"sin encontrar un pool de PumpSwap. Abortando esta entrada.")
                        return None

                    logger.debug(f"[Feed en vivo] pasaron {self.cfg.live_feed_timeout_seconds:.0f}s desde el "
                                 f"ack sin ningún trade real -> probablemente este mint ya migró a PumpSwap "
                                 f"y subscribeTokenTrade no lo cubre. Probando fallback on-chain...")
                    price, pool_confirmed_absent = await self._try_onchain_fallback()
                    if price is not None:
                        return price
                    if pool_confirmed_absent:
                        logger.info(f"[On-chain PumpSwap] Confirmado: todavía no hay pool de PumpSwap "
                                     f"para este mint -sigue en bonding curve, probablemente solo poco "
                                     f"volumen-. Sigo esperando el feed en vivo (hasta "
                                     f"{self.cfg.entry_wait_timeout_seconds:.0f}s en total)...")
                        ack_received_at = time.monotonic()  # reinicia la ventana antes del próximo intento
                        continue
                    logger.error("[On-chain PumpSwap] Tampoco se pudo obtener precio on-chain para este "
                                 "mint. No hay ninguna fuente de precio disponible; abortando esta entrada.")
                    return None

            try:
                event = await self._next_trade_event(timeout=timeout)
            except _ShutdownRequested:
                logger.info("Cancelado por el usuario mientras se esperaba el precio de entrada.")
                return None
            except asyncio.TimeoutError:
                continue  # se recalcula el timeout restante y dispara el fallback arriba
            except StopAsyncIteration:
                logger.warning("[Feed en vivo] la conexión se cerró antes de recibir un trade con precio.")
                return None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Feed en vivo] la conexión falló: {e}")
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
                                   f"el ack de suscripción. Revisá la conexión de red y que el mint sea correcto.")

    async def _try_onchain_fallback(self) -> tuple[Optional[float], bool]:
        """Consulta puntual al pool de PumpSwap directo de Solana (ver
        PumpSwapOnChainClient en pump.py). Devuelve (price,
        pool_confirmed_absent):
          - price no-None: encontró un pool con precio válido -mint
            migrado de verdad-. Marca `_using_onchain_fallback = True`
            para que el monitoreo posterior de la posición (ver
            _poll_onchain_price_loop) también use polling on-chain.
          - price None, pool_confirmed_absent True: confirmado que NO
            hay pool -mint todavía en bonding curve-. El llamador
            (_get_reference_price) decide si sigue esperando el feed
            en vivo.
          - price None, pool_confirmed_absent False: se encontró un
            pool pero no se pudo leer el precio, o la consulta on-chain
            en sí falló -no hay ninguna confirmación útil-. El llamador
            debe abortar, no reintentar el feed en vivo a ciegas."""
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        price, pool_confirmed_absent = await onchain.fetch_price_or_confirm_absent(self.mint)
        if price is not None:
            logger.info(f"Precio de referencia (fallback on-chain PumpSwap): {price:.10f} SOL/token")
            self._using_onchain_fallback = True
        return price, pool_confirmed_absent

    async def _wait_for_dip_entry(self, reference_price: float, target_price: float) -> Optional[float]:
        """Sólo se llama cuando `entry_dip_pct` > 0 (ver _get_initial_price).

        Ya tenemos un precio de REFERENCIA (recién resuelto por
        _get_reference_price) pero todavía NO compramos con él. Acá
        seguimos mirando el precio -por el mismo canal que produjo esa
        referencia: el feed en vivo ya suscripto, o polling on-chain si
        el mint ya migró (_using_onchain_fallback)- hasta que toque
        `target_price` o baje de ahí, y ESE es el precio real de compra.

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

        if self._using_onchain_fallback:
            onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
            while True:
                if self._shutdown_requested.is_set():
                    logger.info("Cancelado por el usuario mientras se esperaba la baja de entrada.")
                    return None
                try:
                    price = await onchain.fetch_price_for_migrated_mint(self.mint)
                except Exception as e:
                    logger.warning(f"[On-chain PumpSwap] error puntual esperando la baja de entrada, "
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
                except asyncio.TimeoutError:
                    continue  # se cumplió el intervalo de polling sin pedido de shutdown; seguimos

        # Feed en vivo: reusamos la misma conexión/suscripción ya abierta.
        # Si se corta, reconectamos igual que hace _consume_trade_stream,
        # porque todavía no hay posición abierta que ese loop pueda cubrir.
        #
        # OJO: el log periódico de progreso corre en una tarea de fondo
        # aparte (_dip_progress_logger), NO metiendo un timeout en
        # _next_trade_event() para "despertarnos" cada tanto. Meterle un
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
                    event = await self._next_trade_event()
                except _ShutdownRequested:
                    logger.info("Cancelado por el usuario mientras se esperaba la baja de entrada.")
                    return None
                except asyncio.CancelledError:
                    raise
                except (StopAsyncIteration, Exception) as e:
                    is_clean_close = isinstance(e, StopAsyncIteration)
                    logger.warning(f"[Feed en vivo] {'la conexión se cerró' if is_clean_close else f'conexión interrumpida ({e})'} "
                                   f"mientras se esperaba la baja de entrada; reconectando...")
                    try:
                        if self._ws is not None:
                            try:
                                await self._ws.close()
                            except Exception:
                                pass
                        self._ws = await self.client.connect_trade_stream(self.mint)
                        self._trade_events = self.client.iter_trade_events(self._ws)
                        # El generador viejo quedó abandonado de verdad acá
                        # (nueva conexión, no un timeout) -cualquier tarea
                        # pendiente de su __anext__() ya no sirve.
                        if self._pending_next_event_task is not None:
                            self._pending_next_event_task.cancel()
                            self._pending_next_event_task = None
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
            await asyncio.sleep(self.cfg.status_interval_seconds)
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
        logger.info(f"⏳ Esperando la baja de entrada | precio actual {self.latest_price:.10f} "
                    f"| objetivo {target_price:.10f} | falta bajar {falta_pct:.2f}% más")

    async def _poll_onchain_price_loop(self) -> None:
        """Reemplazo de _consume_trade_stream para cuando la posición se
        abrió vía el fallback on-chain: como subscribeTokenTrade no
        entrega nada para este mint, no hay forma de enterarse de nuevos
        precios por el feed en vivo -así que se consulta on-chain cada
        `onchain_poll_interval_seconds` mientras la posición siga
        abierta, y se alimenta al mismo _on_price_update() que usaría el
        feed en vivo (misma lógica de trailing-stop/stop-loss, solo
        cambia de dónde sale el precio).

        Un error puntual de RPC (timeout, rate limit, etc.) NO debe matar
        este loop para siempre -eso dejaría el precio congelado igual que
        el bug que tenía _consume_trade_stream-, así que cada iteración
        atrapa sus propios errores y sigue reintentando en el próximo
        ciclo."""
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        while self.position is not None and not self.position.closed:
            try:
                price = await onchain.fetch_price_for_migrated_mint(self.mint)
                if price is not None and price > 0:
                    self.latest_price = price
                    await self._on_price_update(price)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[Polling on-chain PumpSwap] error puntual, reintento en el "
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
        de acá abajo ni el StopAsyncIteration se enteraban -el precio
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
                        event = await self._next_trade_event(timeout=self.cfg.stall_timeout_seconds)
                    except asyncio.TimeoutError:
                        if await self._handle_feed_stall():
                            return  # migró: _handle_feed_stall ya corrió el polling on-chain hasta el cierre
                        continue  # solo poco volumen: seguimos esperando el feed en vivo
                    price = self.client.extract_price(event)
                    if price is None or price <= 0:
                        continue
                    self.latest_price = price
                    await self._on_price_update(price)
                    backoff = 2.0  # se recibió un evento bueno: reseteamos el backoff
                # (inalcanzable: el while True interno solo se sale por return)
            except _ShutdownRequested:
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
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                self._ws = await self.client.connect_trade_stream(self.mint)
                self._trade_events = self.client.iter_trade_events(self._ws)
                # Idem: generador viejo abandonado de verdad, no por timeout.
                if self._pending_next_event_task is not None:
                    self._pending_next_event_task.cancel()
                    self._pending_next_event_task = None
                logger.info("Reconectado al feed de trades de PumpPortal.")
            except Exception as e:
                logger.warning(f"No se pudo reconectar todavía: {e}. Reintento en {backoff:.0f}s...")

    async def _handle_feed_stall(self) -> bool:
        """Se llama cuando pasaron `stall_timeout_seconds` sin ningún
        trade nuevo del feed en vivo, con la posición ya abierta.
        Confirma con UNA consulta on-chain puntual si el mint ya migró
        a PumpSwap:

          - Si hay un pool con precio válido: es una migración real (no
            solo una pausa de volumen). Aplica ese precio de una, marca
            `_using_onchain_fallback = True` y corre el polling on-chain
            (_poll_onchain_price_loop) hasta que la posición se cierre
            -> devuelve True (el llamador debe dejar de esperar el feed
            en vivo, que ya sabemos que no va a entregar nada más para
            este mint).
          - Si no hay pool (o falla la consulta): probablemente es solo
            un token con poco volumen momentáneo -> devuelve False y el
            llamador sigue esperando el feed en vivo normalmente."""
        if self.position is None or self.position.closed:
            return True
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        price = await onchain.fetch_price_for_migrated_mint(self.mint)
        if price is None or price <= 0:
            logger.debug(f"[Feed de trades de PumpPortal] sin trades nuevos hace "
                         f"{self.cfg.stall_timeout_seconds:.0f}s, pero no se encontró (todavía) un pool "
                         f"de PumpSwap con precio válido -> probablemente solo poco volumen, sigo "
                         f"esperando el feed en vivo.")
            return False
        logger.warning(f"[Feed de trades de PumpPortal] sin trades nuevos hace "
                       f"{self.cfg.stall_timeout_seconds:.0f}s y se confirmó un pool de PumpSwap con "
                       f"precio válido -> el mint migró a mitad de la posición (subscribeTokenTrade no "
                       f"lo va a cubrir más). Paso a polling on-chain cada "
                       f"{self.cfg.onchain_poll_interval_seconds:.0f}s para no perder el precio.")
        self._using_onchain_fallback = True
        self.latest_price = price
        await self._on_price_update(price)
        if self.position is None or self.position.closed:
            return True
        await self._poll_onchain_price_loop()
        return True

    async def _status_printer_loop(self) -> None:
        """Imprime el %% de profit actual cada `status_interval_seconds`, sin
        depender de que lleguen nuevas operaciones del token en ese momento.
        Este es el ÚNICO lugar que imprime el estado de forma periódica."""
        while True:
            await asyncio.sleep(self.status_interval_seconds)
            if self.position is None or self.position.closed or self.latest_price is None:
                continue
            pos = self.position
            pnl = pos.pnl_pct(self.latest_price)
            if pos.armed:
                stop_price = pos.highest_price * (1 - self.cfg.trailing_pct / 100.0)
                logger.info(f"⏱️  [armado] precio {self.latest_price:.10f} | máximo {pos.highest_price:.10f} "
                            f"| nivel de venta {stop_price:.10f} | PnL: {pnl:+.2f}%")
            else:
                logger.info(f"⏱️  [esperando activación +{self.cfg.activation_pct}%] "
                            f"precio {self.latest_price:.10f} | entrada {pos.entry_price:.10f} | PnL: {pnl:+.2f}%")

    async def _on_first_price(self, price: float) -> None:
        self.position = await self.executor.buy(self.mint, price)
        logger.info(f"Activación del trailing-stop: +{self.cfg.activation_pct}% "
                    f"| ancho del trailing una vez armado: {self.cfg.trailing_pct}% "
                    f"| stop-loss inicial (antes de armar): -{self.cfg.initial_stop_pct}%")

    async def _on_price_update(self, price: float) -> None:
        pos = self.position
        if pos is None or pos.closed:
            return
        pnl = pos.pnl_pct(price)

        # --- Caso 1: todavía no se armó el trailing-stop ------------------ #
        if not pos.armed:
            if price >= pos.entry_price * (1 + self.cfg.activation_pct / 100.0):
                pos.armed = True
                pos.highest_price = price
                logger.info(f"✅ Trailing-stop ARMADO. Precio actual {price:.10f} "
                            f"(PnL {pnl:+.2f}%). Máximo inicial registrado.")
            elif price <= pos.entry_price * (1 - self.cfg.initial_stop_pct / 100.0):
                await self._try_sell(pos, price, "stop-loss inicial (nunca se activó el trailing)")
            return

        # --- Caso 2: trailing-stop armado, sigue el máximo ----------------- #
        if price > pos.highest_price:
            pos.highest_price = price
            stop_price = pos.highest_price * (1 - self.cfg.trailing_pct / 100.0)
            logger.info(f"📈 Nuevo máximo: {price:.10f} (PnL {pnl:+.2f}%) "
                        f"| nuevo nivel de venta (trailing): {stop_price:.10f}")
            return

        stop_price = pos.highest_price * (1 - self.cfg.trailing_pct / 100.0)
        if price <= stop_price:
            await self._try_sell(
                pos, price,
                f"retroceso de {self.cfg.trailing_pct}% desde el máximo ({pos.highest_price:.10f})"
            )

    async def _try_sell(self, pos: Position, price: float, reason: str) -> None:
        """Envuelve executor.sell(): si la venta REAL falla (Lightning API
        devuelve error, o la tx confirma pero FALLA on-chain -p. ej. por
        slippage excedido-), executor.sell() ahora propaga la excepción a
        propósito en vez de marcar la posición como cerrada (ver BUGFIX en
        executor.py). Acá la atajamos para que ese fallo:
          - se loguee como lo que es (venta fallida), no como "conexión
            interrumpida" (que es lo que pasaría si se colara hasta el
            except genérico de _consume_trade_stream), y
          - NO trabe el bot para siempre: como la posición sigue abierta
            (closed=False) y NO seteamos _closed_event, el próximo trade
            que llegue vuelve a evaluar la condición de salida y reintenta
            la venta sola, sin intervención manual."""
        async with self._sell_lock:
            # Revalidamos DESPUÉS de conseguir el lock: si _sell_on_shutdown
            # (u otra llamada) ya vendió mientras esperábamos acá, esto ya
            # no corresponde -evita el segundo pedido real a la Lightning API.
            if pos.closed:
                return
            try:
                await self.executor.sell(pos, price, reason)
            except Exception as e:
                logger.warning(f"⚠️  Venta fallida ({reason}): {e}. La posición SIGUE ABIERTA, "
                               f"se reintentará con el próximo precio que llegue.")
                return
        self._closed_event.set()
