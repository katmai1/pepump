import asyncio
import contextlib
import logging
import signal
import time
from typing import AsyncIterator, Optional

from pepump.executor import Position
from pepump.pump import PumpSwapOnChainClient, PumpCurveOnChainClient, RaydiumCpmmOnChainClient

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
# default son 20s de ceguera por precio. Se reinicia en cuanto vuelve
# a llegar un trade real (ver _consume_trade_stream).
_MAX_CURVE_STALLS_BEFORE_POLLING = 2

# Mientras el chequeo temprano de existencia on-chain (ver
# _confirm_mint_not_pumpfun / _get_reference_price) todavía no terminó,
# cada espera de un trade del feed en vivo se corta en rebanadas de
# esta duración en vez de esperar de una todo `live_feed_timeout_seconds`
# -así, apenas ese chequeo confirma que el mint no es de pump.fun, el
# bot aborta en el próximo ciclo del loop en vez de recién después de
# los live_feed_timeout_seconds completos (pensados para tolerar
# tokens de poco volumen, no para esto).
_EXISTENCE_CHECK_POLL_SECONDS = 0.5


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
        # Si tuvimos que resolver el precio (de entrada, o durante el
        # monitoreo de una posición ya abierta) por un fallback on-chain
        # en vez del feed en vivo de PumpPortal -> qué fallback está
        # activo ahora mismo, para que el resto del código sepa con qué
        # cliente/lógica seguir consultando (ver _onchain_fetch_price,
        # _poll_onchain_price_loop, _wait_for_dip_entry,
        # _resolve_shutdown_price):
        #   None           -> no se usó ningún fallback, todo por el feed en vivo.
        #   "bondingcurve" -> el mint sigue en bonding curve pero
        #                     subscribeTokenTrade no entregó nada (ack
        #                     recibido, sin trades reales); se lee el
        #                     precio directo de la cuenta de la bonding
        #                     curve on-chain (ver PumpCurveOnChainClient).
        #   "pumpswap"     -> el mint ya migró a PumpSwap; se lee el
        #                     precio directo del pool on-chain (ver
        #                     PumpSwapOnChainClient).
        self._onchain_source: Optional[str] = None
        # Si _get_reference_price (o algo que llame desde ahí) ya
        # logueó un motivo específico para devolver None -mint que no es
        # de pump.fun, timeout total, fallback on-chain sin precio,
        # conexión cortada, etc.-, se marca acá para que el caller en
        # run() NO agregue ENCIMA el mensaje genérico de "se cortó la
        # conexión, verificá la dirección y la api_key": antes se
        # logueaban los dos, uno específico y después uno genérico que
        # podía contradecirlo (ej. "no es un token de pump.fun" seguido
        # de "verificá la api_key"), muy confuso para el usuario.
        self._initial_price_failure_reason_logged = False
        # Stalls SEGUIDOS del feed en vivo con la bonding curve todavía
        # activa (ver _handle_feed_stall y _MAX_CURVE_STALLS_BEFORE_POLLING).
        self._curve_stalls = 0
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
                elif not self._initial_price_failure_reason_logged:
                    # Solo llegamos acá si no se logueó ningún motivo
                    # específico arriba (ver _initial_price_failure_reason_logged):
                    # un caso realmente no cubierto por los diagnósticos de
                    # _get_reference_price. Si ya se logueó un motivo
                    # puntual (mint que no es de pump.fun, timeout total,
                    # fallback on-chain sin precio, etc.) NO lo repetimos acá
                    # con un mensaje genérico que podría contradecirlo.
                    logger.error("Se cortó la conexión con PumpPortal antes de recibir un trade con "
                                 "precio. Verificá la dirección del token y la api_key, y volvé a intentar.")
                return

            self.latest_price = initial_price
            try:
                await self._on_first_price(initial_price)
            except Exception as e:
                logger.error(f"La compra no se confirmó on-chain, no se abrió ninguna posición: {e}")
                return

            if self._onchain_source is not None:
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
            # se cancelan acá ANTES de vender, así que cuando corre
            # _sell_on_shutdown ya no hay nada más que pueda pisarle la venta.
            need_shutdown_sell = await self._wait_for_close_or_shutdown(monitor_task)

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
                # Un fallo al cerrar (socket ya roto del otro lado) no
                # puede reventar el cierre del bot: si esto tira, la
                # excepción sale de run() por el finally y run.py -que
                # solo atrapa KeyboardInterrupt- muere con un traceback
                # crudo DESPUÉS de una operación que salió bien.
                try:
                    await self._ws.close()
                except Exception as e:
                    logger.debug(f"No se pudo cerrar prolijamente el websocket: {e}")

        logger.info("Bot finalizado.")

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
        GC-, así que el bot se quedaba acá esperando para siempre un
        evento de cierre que ya no podía llegar, con SOL real
        comprometido y sin vigilar el precio. La única salida era un
        Ctrl+C a mano. Ahora la tarea de monitoreo también entra en la
        espera: si termina antes de que la posición se cierre, se
        loguea el motivo y se sale vendiendo, que es lo seguro.

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
        esperando = {closed_task, shutdown_task}
        if monitor_task is not None:
            esperando.add(monitor_task)
        try:
            await asyncio.wait(esperando, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # OJO: monitor_task NO se cancela acá -de eso se encarga
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
                await self.executor.sell(self.position, price, "cierre manual (Ctrl+C/SIGTERM)",
                                          pool_override=self._current_pool_override())
            except Exception as e:
                logger.error(f"Falló la venta de cierre manual: {e}. La posición SIGUE ABIERTA — "
                             f"revisala manualmente: https://pump.fun/{self.mint}")

    async def _resolve_shutdown_price(self) -> Optional[float]:
        if self._onchain_source is not None:
            try:
                price = await self._onchain_fetch_price()
            except Exception as e:
                logger.debug(f"[On-chain] Falló la consulta fresca al cerrar: {e}")
                price = None
            if price is not None and price > 0:
                return price
            logger.debug("[On-chain] No se pudo refrescar el precio al cerrar; "
                         "uso el último precio conocido.")
        return self.latest_price

    async def _onchain_fetch_price(self) -> Optional[float]:
        """Consulta puntual al fallback on-chain ACTUALMENTE activo
        (self._onchain_source), usada para refrescar/pollear el precio
        de una posición ya resuelta por ese fallback (ver
        _poll_onchain_price_loop, _resolve_shutdown_price,
        _wait_for_dip_entry).

        Si el fallback activo es la bonding curve y ésta ya completó
        (migró mientras estábamos pollando), pasa automáticamente al
        fallback de PumpSwap para ESTA MISMA consulta y deja
        `self._onchain_source` en "pumpswap" para las próximas -mismo
        espíritu que _handle_feed_stall detectando una migración a
        mitad de posición, pero acá para el caso en que ya veníamos
        on-chain por bonding curve."""
        if self._onchain_source == "raydium-cpmm":
            cpmm = RaydiumCpmmOnChainClient(self.cfg.solana_rpc_url)
            return await cpmm.fetch_price(self.mint)
        if self._onchain_source == "bondingcurve":
            curve = PumpCurveOnChainClient(self.cfg.solana_rpc_url)
            price, complete, exists = await curve.fetch_price_or_status(self.mint)
            if exists and complete:
                logger.info("[On-chain bonding curve] La curva completó (migró) mientras se hacía "
                            "polling -> paso al fallback de PumpSwap.")
                self._onchain_source = "pumpswap"
                swap = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
                return await swap.fetch_price_for_migrated_mint(self.mint)
            return price
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        return await onchain.fetch_price_for_migrated_mint(self.mint)

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
        # BUGFIX (evento perdido tras un stall): si la tarea que quedó
        # pendiente de un timeout anterior YA terminó, su resultado es un
        # trade real que llegó mientras hacíamos otra cosa (típicamente
        # las consultas RPC de _handle_feed_stall, que tardan). Antes se
        # descartaba y se creaba un __anext__() nuevo: ese trade se
        # perdía -y con él, una evaluación del trailing-stop y el reset
        # de self._curve_stalls-. Si terminó con excepción (conexión
        # cerrada), .result() la propaga acá y el llamador reconecta, en
        # vez de quedar como "Task exception was never retrieved".
        pending = self._pending_next_event_task
        if pending is not None and pending.done() and not pending.cancelled():
            self._pending_next_event_task = None
            if self._shutdown_requested.is_set():
                # Se pidió apagado mientras tanto: no devolvemos un
                # precio que dispararía una compra justo al salir.
                raise _ShutdownRequested()
            return pending.result()

        if pending is None or pending.cancelled():
            next_task = asyncio.ensure_future(self._trade_events.__anext__())
        else:
            next_task = pending
        shutdown_task = asyncio.ensure_future(self._shutdown_requested.wait())
        done, _pending = await asyncio.wait(
            {next_task, shutdown_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )

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

        En PARALELO con lo anterior, desde el arranque, corre
        _confirm_mint_not_pumpfun() en segundo plano (ver ese método):
        confirma cuanto antes -sin esperar ningún timeout pensado para
        tolerar tokens de poco volumen- si el mint directamente NO es de
        pump.fun/PumpSwap (ni bonding curve ni pool, ambas consultas
        confirmando ausencia). Es una pregunta que no depende de cuánto
        volumen tenga el token, así que no tiene sentido esperar a
        `live_feed_timeout_seconds` para hacerla.

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

        existence_check_task: Optional[asyncio.Task] = asyncio.ensure_future(
            self._confirm_mint_not_pumpfun()
        )
        try:
            while True:
                if existence_check_task is not None and existence_check_task.done():
                    mint_not_pumpfun = existence_check_task.result()
                    existence_check_task = None
                    if mint_not_pumpfun:
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
                        price, pool_confirmed_absent, mint_not_pumpfun = await self._try_onchain_fallback()
                        if price is not None:
                            return price
                        if mint_not_pumpfun:
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
                        # Todavía no se resolvió el chequeo temprano de
                        # existencia: en vez de bloquearnos acá hasta
                        # los live_feed_timeout_seconds completos,
                        # cortamos la espera en rebanadas cortas para
                        # poder revisarlo (arriba, al volver al inicio
                        # del loop) y abortar apenas confirme que el
                        # mint no es de pump.fun.
                        timeout = min(timeout, _EXISTENCE_CHECK_POLL_SECONDS)

                try:
                    event = await self._next_trade_event(timeout=timeout)
                except _ShutdownRequested:
                    logger.info("Cancelado por el usuario mientras se esperaba el precio de entrada.")
                    return None
                except asyncio.TimeoutError:
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
                                       f"el ack de suscripción. Revisá la conexión de red y que el mint sea correcto.")
        finally:
            if existence_check_task is not None and not existence_check_task.done():
                existence_check_task.cancel()
                with contextlib.suppress(BaseException):
                    await existence_check_task

    def _log_mint_not_pumpfun_abort(self) -> None:
        """Único mensaje de error para el caso "este mint no es de
        pump.fun/PumpSwap" -antes se logueaba una vez en
        _try_onchain_fallback (o en el chequeo temprano) Y OTRA VEZ acá,
        con textos parecidos pero no idénticos, lo que se leía como dos
        errores mezclados para un solo problema."""
        logger.error(f"Abortando esta entrada: no hay de dónde sacar el precio de {self.mint} "
                     f"(no tiene bonding curve de pump.fun, ni pool oficial de migración en "
                     f"PumpSwap, ni pool de Raydium CPMM contra SOL con liquidez real). Si está en "
                     f"otro DEX (Meteora, Raydium AMM v4/CLMM, LaunchLab sin graduar...), este bot "
                     f"no puede calcular su precio de entrada.")

    async def _confirm_mint_not_pumpfun(self) -> bool:
        """Chequeo temprano y SIN efectos secundarios (no toca
        self._onchain_source, a diferencia de _try_onchain_fallback):
        confirma lo antes posible si este mint NUNCA se lanzó en
        pump.fun, consultando en paralelo con la espera del feed en vivo
        desde el arranque de _get_reference_price -en vez de recién
        después de `live_feed_timeout_seconds`, que existe para tolerar
        tokens de poco volumen y no tiene nada que ver con esta pregunta.

        Devuelve True solo si TODAS las consultas on-chain (PumpSwap,
        bonding curve y Raydium CPMM) respondieron bien y confirmaron
        ausencia -nunca ante un fallo de RPC (ver PumpSwapOnChainClient.
        fetch_price_or_confirm_absent y PumpCurveOnChainClient.
        fetch_price_or_status), para no abortar una entrada válida por
        un problema transitorio de conexión."""
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        _, pool_confirmed_absent = await onchain.fetch_price_or_confirm_absent(self.mint)
        if not pool_confirmed_absent:
            return False
        curve = PumpCurveOnChainClient(self.cfg.solana_rpc_url)
        _, _, exists = await curve.fetch_price_or_status(self.mint)
        if exists is not False:
            return False
        cpmm = RaydiumCpmmOnChainClient(self.cfg.solana_rpc_url)
        _, cpmm_confirmed_absent = await cpmm.fetch_price_or_confirm_absent(self.mint)
        return cpmm_confirmed_absent

    async def _try_onchain_fallback(self) -> tuple[Optional[float], bool, bool]:
        """Consulta puntual a los fallbacks on-chain, en dos pasos:

        1. PumpSwap (ver PumpSwapOnChainClient): si aparece un pool con
           precio válido, el mint ya migró de verdad -se usa ese precio.

        2. Si PumpSwap confirma que NO hay pool (`pool_confirmed_absent`
           -el mint sigue en bonding curve), en vez de simplemente
           volver a esperar a ciegas el feed en vivo -que es
           precisamente el que no está entregando nada-, leemos el
           precio DIRECTO de la cuenta de la bonding curve on-chain (ver
           PumpCurveOnChainClient). Si responde con un precio válido
           (curva todavía no completada), lo usamos como referencia y
           quedamos en modo polling on-chain por bonding curve para el
           resto de la posición.

        Devuelve (price, pool_confirmed_absent, mint_not_pumpfun):
          - price no-None: se resolvió por alguno de los dos fallbacks
            -`self._onchain_source` queda seteado ("pumpswap",
            "bondingcurve" o "raydium-cpmm") para que el monitoreo posterior de la
            posición (ver _poll_onchain_price_loop) sepa con cuál seguir.
          - price None, pool_confirmed_absent True, mint_not_pumpfun
            True: NI el pool de PumpSwap NI la cuenta de bonding curve
            existen para este mint (ambas consultas respondieron bien y
            confirmaron ausencia, no un fallo de RPC) -> este mint NUNCA
            se lanzó en pump.fun (ej. un mint nativo de Raydium/
            Meteora/otro DEX). No tiene sentido seguir esperando el feed
            en vivo ni reintentar este fallback: nunca va a aparecer
            nada acá. El llamador (_get_reference_price) debería
            abortar de una en vez de esperar hasta el timeout.
          - price None, pool_confirmed_absent True, mint_not_pumpfun
            False: la bonding curve existe pero no dio un precio
            utilizable (ya completada y el pool de PumpSwap todavía no
            está indexado, cuenta con datos ilegibles, o falló la
            consulta on-chain en sí -RPC caído-). El llamador
            (_get_reference_price) decide si sigue esperando el feed en
            vivo un ciclo más.
          - price None, pool_confirmed_absent False: la consulta de
            PumpSwap en sí no dio ninguna confirmación útil (se
            encontró un pool pero no se pudo leer, o falló la query) -
            no hay nada más que probar acá, el llamador debe abortar en
            vez de reintentar a ciegas."""
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        price, pool_confirmed_absent = await onchain.fetch_price_or_confirm_absent(self.mint)
        if price is not None:
            logger.info(f"Precio de referencia (fallback on-chain PumpSwap): {price:.10f} SOL/token")
            self._onchain_source = "pumpswap"
            return price, pool_confirmed_absent, False

        if pool_confirmed_absent:
            curve = PumpCurveOnChainClient(self.cfg.solana_rpc_url)
            curve_price, complete, exists = await curve.fetch_price_or_status(self.mint)
            if curve_price is not None and not complete:
                logger.info(f"Precio de referencia (fallback on-chain bonding curve): "
                            f"{curve_price:.10f} SOL/token")
                self._onchain_source = "bondingcurve"
                return curve_price, pool_confirmed_absent, False
            if exists and complete:
                logger.debug("[On-chain bonding curve] La curva ya completó (migrando a PumpSwap) "
                             "pero el pool de PumpSwap todavía no aparece indexado. Reintento en el "
                             "próximo ciclo.")
            elif exists is False:
                # Ni bonding curve ni pool oficial de PumpSwap: no es un
                # token de pump.fun. Último intento: Raydium CPMM contra
                # SOL (ej. tokens de bonk.fun/LaunchLab ya graduados).
                cpmm = RaydiumCpmmOnChainClient(self.cfg.solana_rpc_url)
                cpmm_price, cpmm_confirmed_absent = await cpmm.fetch_price_or_confirm_absent(self.mint)
                if cpmm_price is not None:
                    logger.info(f"Precio de referencia (fallback on-chain Raydium CPMM): "
                                f"{cpmm_price:.10f} SOL/token")
                    self._onchain_source = "raydium-cpmm"
                    return cpmm_price, pool_confirmed_absent, False
                if cpmm_confirmed_absent:
                    # Mensaje de error único para este caso: lo loguea el
                    # llamador (_get_reference_price._log_mint_not_pumpfun_abort),
                    # no acá -de lo contrario saldrían dos ERROR casi
                    # idénticos para un solo problema (este chequeo normalmente
                    # ya lo detectó antes vía _confirm_mint_not_pumpfun).
                    logger.debug(f"[On-chain] {self.mint} no tiene bonding curve de pump.fun, NI pool "
                                 f"de PumpSwap, NI pool de Raydium CPMM utilizable (las tres consultas "
                                 f"confirmaron ausencia).")
                    return price, pool_confirmed_absent, True

        return price, pool_confirmed_absent, False

    async def _wait_for_dip_entry(self, reference_price: float, target_price: float) -> Optional[float]:
        """Sólo se llama cuando `entry_dip_pct` > 0 (ver _get_initial_price).

        Ya tenemos un precio de REFERENCIA (recién resuelto por
        _get_reference_price) pero todavía NO compramos con él. Acá
        seguimos mirando el precio -por el mismo canal que produjo esa
        referencia: el feed en vivo ya suscripto, o polling on-chain si
        se resolvió por algún fallback (self._onchain_source)- hasta que
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

        if self._onchain_source is not None:
            while True:
                if self._shutdown_requested.is_set():
                    logger.info("Cancelado por el usuario mientras se esperaba la baja de entrada.")
                    return None
                try:
                    price = await self._onchain_fetch_price()
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
                except Exception as e:
                    # StopAsyncIteration (cierre "limpio" del server) ya
                    # entra por acá: es subclase de Exception. Solo se
                    # distingue para el texto del log.
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
        abrió (o se detectó una migración a mitad de posición, ver
        _handle_feed_stall) vía algún fallback on-chain: como
        subscribeTokenTrade no está entregando nada útil para este
        mint, no hay forma de enterarse de nuevos precios por el feed en
        vivo -así que se consulta on-chain cada
        `onchain_poll_interval_seconds` mientras la posición siga
        abierta (bonding curve o PumpSwap, según `self._onchain_source`
        -ver _onchain_fetch_price, que también se encarga de pasar de
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
                price = await self._onchain_fetch_price()
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
                    # El feed volvió a entregar: los stalls acumulados ya
                    # no cuentan como "el feed está muerto para este mint".
                    self._curve_stalls = 0
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
        Confirma con consultas on-chain puntuales qué está pasando:

          1. Primero chequea si hay un pool de PumpSwap con precio
             válido -es una migración real (no solo una pausa de
             volumen). Si lo hay: aplica ese precio de una, marca
             `_onchain_source = "pumpswap"` y corre el polling on-chain
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
             devuelve False igual, pero con self.latest_price ya
             refrescado en vez de congelado.

          3. Si ni el pool ni la bonding curve dan nada legible ->
             probablemente es solo un token con muy poco volumen
             momentáneo -> devuelve False sin tocar el precio."""
        if self.position is None or self.position.closed:
            return True
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        price = await onchain.fetch_price_for_migrated_mint(self.mint)
        if price is not None and price > 0:
            logger.warning(f"[Feed de trades de PumpPortal] sin trades nuevos hace "
                           f"{self.cfg.stall_timeout_seconds:.0f}s y se confirmó un pool de PumpSwap "
                           f"con precio válido -> el mint migró a mitad de la posición "
                           f"(subscribeTokenTrade no lo va a cubrir más). Paso a polling on-chain cada "
                           f"{self.cfg.onchain_poll_interval_seconds:.0f}s para no perder el precio.")
            self._onchain_source = "pumpswap"
            self.latest_price = price
            await self._on_price_update(price)
            if self.position is None or self.position.closed:
                return True
            await self._poll_onchain_price_loop()
            return True

        curve = PumpCurveOnChainClient(self.cfg.solana_rpc_url)
        curve_price, complete, exists = await curve.fetch_price_or_status(self.mint)
        if curve_price is not None and not complete:
            self._curve_stalls += 1
            if self._curve_stalls >= _MAX_CURVE_STALLS_BEFORE_POLLING:
                # El feed lleva varios ciclos sin entregar NADA para un
                # mint que sigue perfectamente vivo en la curva. Seguir
                # esperándolo significa evaluar el trailing-stop una vez
                # cada stall_timeout_seconds (20s por defecto): demasiado
                # lento para una memecoin. Pasamos a polling on-chain de
                # la bonding curve, que es la MISMA fuente contra la que
                # se ejecuta el trade, cada onchain_poll_interval_seconds.
                logger.warning(f"[Feed de trades de PumpPortal] {self._curve_stalls} ciclos seguidos "
                               f"sin trades ({self.cfg.stall_timeout_seconds:.0f}s cada uno) con la "
                               f"bonding curve todavía activa -> dejo de depender del feed en vivo "
                               f"para esta posición y paso a leer el precio de la curva on-chain cada "
                               f"{self.cfg.onchain_poll_interval_seconds:.0f}s.")
                self._onchain_source = "bondingcurve"
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

    async def _status_printer_loop(self) -> None:
        """Imprime el %% de profit actual cada `status_interval_seconds`, sin
        depender de que lleguen nuevas operaciones del token en ese momento.
        Este es el ÚNICO lugar que imprime el estado de forma periódica."""
        while True:
            await asyncio.sleep(self.status_interval_seconds)
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
                    if pos.entry_is_real_fill else "")
            if pos.armed:
                stop_price = pos.highest_price * (1 - self.cfg.trailing_pct / 100.0)
                logger.info(f"⏱️  [armado] precio {self.latest_price:.10f} | máximo {pos.highest_price:.10f} "
                            f"| nivel de venta {stop_price:.10f} | mercado: {pnl:+.2f}%{neto}")
            else:
                # `entrada` es el precio de MERCADO contra el que se mide
                # la activación; el precio EFECTIVO pagado (con
                # comisiones) se muestra aparte para que no parezca un
                # error ver el neto en negativo con el precio clavado en
                # la entrada.
                coste = (f" | coste real {pos.entry_price:.10f}" if pos.entry_is_real_fill else "")
                logger.info(f"⏱️  [esperando activación +{self.cfg.activation_pct}%] "
                            f"precio {self.latest_price:.10f} | entrada {pos.market_entry_price:.10f}"
                            f"{coste} | mercado: {pnl:+.2f}%{neto}")

    def _current_pool_override(self) -> Optional[str]:
        """Si ya confirmamos -sea al entrar (_try_onchain_fallback) o a
        mitad de posición (_handle_feed_stall)- que este mint migró de
        la bonding curve a PumpSwap, hay que decírselo explícito a la
        Lightning API con pool="pump-amm" en vez de confiar en
        self.cfg.pool="auto".

        Motivo: pool="auto" le pide a PumpPortal que resuelva solo por
        dónde rutear la orden, y esa resolución puede quedar pisada con
        la bonding curve vieja para un mint recién migrado. Ahí la orden
        revierte on-chain con el error 6005 (BondingCurveComplete) del
        programa Pump, porque esa curva ya no existe para este mint -
        aunque nosotros ya confirmamos el pool de PumpSwap on-chain.

        OJO: esto es DISTINTO de `self._onchain_source == "bondingcurve"`
        -ese caso es el mint SIGUE en bonding curve (solo cambió de
        dónde sacamos el precio, no de dónde hay que rutear el trade),
        así que ahí NO hay que overridear nada -> None, se deja que
        cfg.pool decida como siempre. Overridear a "pump-amm" en ese
        caso rompería exactamente el mismo BondingCurveComplete que este
        override existe para evitar, pero al revés."""
        # Raydium CPMM: se rutea explícito al mismo tipo de pool del que
        # sacamos el precio, en vez de dejar que "auto" lo adivine.
        return {"pumpswap": "pump-amm", "raydium-cpmm": "raydium-cpmm"}.get(self._onchain_source)

    async def _on_first_price(self, price: float) -> None:
        self.position = await self.executor.buy(self.mint, price, pool_override=self._current_pool_override())
        logger.info(f"Activación del trailing-stop: +{self.cfg.activation_pct}% "
                    f"| ancho del trailing una vez armado: {self.cfg.trailing_pct}% "
                    f"| stop-loss inicial (antes de armar): -{self.cfg.initial_stop_pct}%")

    async def _on_price_update(self, price: float) -> None:
        pos = self.position
        if pos is None or pos.closed:
            return
        # Movimiento de MERCADO desde la compra: es lo que miden los
        # umbrales de acá abajo y lo que muestra pump.fun. El neto (con
        # los costes de entrada) lo imprime el status printer aparte.
        pnl = pos.market_pnl_pct(price)

        # --- Caso 1: todavía no se armó el trailing-stop ------------------ #
        #
        # OJO: los umbrales se comparan contra `market_entry_price` (el
        # precio de MERCADO al comprar), no contra `entry_price`. Con
        # datos reales de fill, entry_price es el precio EFECTIVO pagado
        # e incluye comisiones + priority fee + el rent de la ATA (sobre
        # una compra de 0.05 SOL eso puede ser un 4-5%): usarlo acá
        # subiría de tapadillo el listón de activación y aflojaría el
        # stop-loss en esa misma proporción, cambiando la estrategia sin
        # que nadie lo haya pedido. El PnL sí usa entry_price -ver
        # Position.pnl_pct-, que es donde el coste real corresponde.
        if not pos.armed:
            if price >= pos.market_entry_price * (1 + self.cfg.activation_pct / 100.0):
                pos.armed = True
                pos.highest_price = price
                logger.info(f"✅ Trailing-stop ARMADO. Precio actual {price:.10f} "
                            f"(PnL {pnl:+.2f}%). Máximo inicial registrado.")
            elif price <= pos.market_entry_price * (1 - self.cfg.initial_stop_pct / 100.0):
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
                await self.executor.sell(pos, price, reason, pool_override=self._current_pool_override())
            except Exception as e:
                logger.warning(f"⚠️  Venta fallida ({reason}): {e}. La posición SIGUE ABIERTA, "
                               f"se reintentará con el próximo precio que llegue.")
                return
        self._closed_event.set()
