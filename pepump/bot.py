import asyncio
import logging
import time
from typing import AsyncIterator, Optional

from pepump.executor import Position
from pepump.pump import PumpSwapOnChainClient

logger = logging.getLogger(__name__)

# Cada cuánto tiempo (segundos), mientras seguimos esperando el primer
# trade real, se repite el recordatorio de diagnóstico con las causas
# más probables (wallet sin fondos, api_key inválida, token sin volumen).
_DIAGNOSTIC_REMINDER_SECONDS = 10.0


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
        # True si tuvimos que resolver el precio de entrada por el
        # fallback on-chain de PumpSwap (ver _try_onchain_fallback) en
        # vez del feed en vivo de PumpPortal -> significa que el mint ya
        # migró y subscribeTokenTrade no entrega nada para él, así que el
        # monitoreo de la posición TAMBIÉN necesita el polling on-chain
        # (ver _poll_onchain_price_loop), no solo el precio inicial.
        self._using_onchain_fallback = False

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
        """
        logger.info(f"Siguiendo el token: {self.mint}")
        logger.debug(f"Suscribiéndose (subscribe_trade) al feed de trades de PumpPortal para {self.mint}...")

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
            self._ws = await self.client.connect_trade_stream(self.mint)
            self._trade_events = self.client.iter_trade_events(self._ws)

            initial_price = await self._get_initial_price()
            if initial_price is None:
                logger.error("Se cortó la conexión con PumpPortal antes de recibir un trade con "
                             "precio. Verificá la dirección del token y la api_key, y volvé a intentar.")
                return

            self.latest_price = initial_price
            self._on_first_price(initial_price)

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

            await self._closed_event.wait()

            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if self._ws is not None:
                await self._ws.close()

        logger.info("Bot finalizado.")

    async def _get_initial_price(self) -> Optional[float]:
        """Consigue el precio de entrada, en este orden:
          1. Feed en vivo de PumpPortal (subscribe_trade) — sin límite de
             tiempo MIENTRAS no haya llegado ni el ack de suscripción
             (eso indicaría un problema de conexión/api_key/wallet, no de
             mint migrado — ver diagnósticos más abajo).
          2. Una vez llega el ack, si no aparece NINGÚN trade real dentro
             de `live_feed_timeout_seconds`, asumimos que el mint ya
             migró a PumpSwap (subscribeTokenTrade no cubre esos casos,
             confirmado a mano) y probamos el fallback on-chain
             (_try_onchain_fallback).

        OJO con el timeout: como puede que NO llegue ningún evento más
        después del ack (ni uno solo), no alcanza con chequear el reloj
        adentro de un `async for` -eso nunca se ejecutaría si no hay
        eventos-, así que se usa asyncio.wait_for() sobre cada
        __anext__() para tener un deadline real incluso en silencio total.
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
                    logger.debug(f"[Feed en vivo] pasaron {self.cfg.live_feed_timeout_seconds:.0f}s desde el "
                                 f"ack sin ningún trade real -> probablemente este mint ya migró a PumpSwap "
                                 f"y subscribeTokenTrade no lo cubre. Probando fallback on-chain...")
                    return await self._try_onchain_fallback()

            try:
                if timeout is not None:
                    event = await asyncio.wait_for(self._trade_events.__anext__(), timeout=timeout)
                else:
                    event = await self._trade_events.__anext__()
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
                logger.info(f"Precio inicial (feed en vivo, subscribe_trade): {price:.10f} SOL/token")
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

    async def _try_onchain_fallback(self) -> Optional[float]:
        """Último recurso para el precio de entrada: lee las reservas
        reales del pool de PumpSwap directo de Solana (ver
        PumpSwapOnChainClient en pump.py). Si funciona, marca
        `_using_onchain_fallback = True` para que el monitoreo posterior
        de la posición (ver _poll_onchain_price_loop) también use polling
        on-chain en vez de esperar al feed en vivo, que ya sabemos que no
        entrega nada para este mint."""
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        price = await onchain.fetch_price_for_migrated_mint(self.mint)
        if price is None:
            logger.error("[On-chain PumpSwap] Tampoco se pudo obtener precio on-chain para este mint. "
                         "No hay ninguna fuente de precio disponible; abortando esta entrada.")
            return None
        logger.info(f"Precio inicial (fallback on-chain PumpSwap): {price:.10f} SOL/token")
        self._using_onchain_fallback = True
        return price

    async def _poll_onchain_price_loop(self) -> None:
        """Reemplazo de _consume_trade_stream para cuando la posición se
        abrió vía el fallback on-chain: como subscribeTokenTrade no
        entrega nada para este mint, no hay forma de enterarse de nuevos
        precios por el feed en vivo -así que se consulta on-chain cada
        `onchain_poll_interval_seconds` mientras la posición siga
        abierta, y se alimenta al mismo _on_price_update() que usaría el
        feed en vivo (misma lógica de trailing-stop/stop-loss, solo
        cambia de dónde sale el precio)."""
        onchain = PumpSwapOnChainClient(self.cfg.solana_rpc_url)
        try:
            while self.position is not None and not self.position.closed:
                price = await onchain.fetch_price_for_migrated_mint(self.mint)
                if price is not None and price > 0:
                    self.latest_price = price
                    self._on_price_update(price)
                await asyncio.sleep(self.cfg.onchain_poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Polling on-chain PumpSwap] interrumpido: {e}")

    async def _consume_trade_stream(self) -> None:
        """Sigue escuchando trades en tiempo real por la MISMA conexión
        websocket ya abierta y suscripta desde run() (subscribe_trade), sin
        reconectar."""
        try:
            async for event in self._trade_events:
                if self.position is None or self.position.closed:
                    break
                price = self.client.extract_price(event)
                if price is None or price <= 0:
                    continue
                self.latest_price = price
                self._on_price_update(price)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Feed de trades de PumpPortal] conexión interrumpida: {e}")

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

    def _on_first_price(self, price: float) -> None:
        self.position = self.executor.buy(self.mint, price)
        logger.info(f"Activación del trailing-stop: +{self.cfg.activation_pct}% "
                    f"| ancho del trailing una vez armado: {self.cfg.trailing_pct}% "
                    f"| stop-loss inicial (antes de armar): -{self.cfg.initial_stop_pct}%")

    def _on_price_update(self, price: float) -> None:
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
                self._try_sell(pos, price, "stop-loss inicial (nunca se activó el trailing)")
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
            self._try_sell(
                pos, price,
                f"retroceso de {self.cfg.trailing_pct}% desde el máximo ({pos.highest_price:.10f})"
            )

    def _try_sell(self, pos: Position, price: float, reason: str) -> None:
        """Envuelve executor.sell(): si la venta REAL falla (Lightning API
        devuelve error), executor.sell() ahora propaga la excepción a
        propósito en vez de marcar la posición como cerrada (ver BUGFIX en
        executor.py). Acá la atajamos para que ese fallo:
          - se loguee como lo que es (venta fallida), no como "conexión
            interrumpida" (que es lo que pasaría si se colara hasta el
            except genérico de _consume_trade_stream), y
          - NO trabe el bot para siempre: como la posición sigue abierta
            (closed=False) y NO seteamos _closed_event, el próximo trade
            que llegue vuelve a evaluar la condición de salida y reintenta
            la venta sola, sin intervención manual."""
        try:
            self.executor.sell(pos, price, reason)
        except Exception as e:
            logger.warning(f"⚠️  Venta fallida ({reason}): {e}. La posición SIGUE ABIERTA, "
                           f"se reintentará con el próximo precio que llegue.")
            return
        self._closed_event.set()
