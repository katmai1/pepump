import asyncio
from typing import AsyncIterator, Optional

from pepump.executor import Position


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
        print(f"Siguiendo el token: {self.mint}")
        print(f"Suscribiéndose (subscribe_trade) al feed de trades de PumpPortal para {self.mint}...")
        self._ws = await self.client.connect_trade_stream(self.mint)
        self._trade_events = self.client.iter_trade_events(self._ws)

        try:
            initial_price = await self._get_initial_price()
            if initial_price is None:
                print("ERROR: se cortó la conexión con PumpPortal antes de recibir un trade con "
                      "precio. Verificá la dirección del token y la api_key, y volvé a intentar.")
                return

            self.latest_price = initial_price
            self._on_first_price(initial_price)

            tasks = [
                asyncio.create_task(self._consume_trade_stream()),
                asyncio.create_task(self._status_printer_loop()),
            ]

            await self._closed_event.wait()

            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._ws.close()

        print("Bot finalizado.")

    async def _get_initial_price(self) -> Optional[float]:
        """Consigue el precio de entrada ÚNICAMENTE del feed en vivo de
        PumpPortal (subscribe_trade), esperando el tiempo que haga falta a
        que llegue el primer trade del mint con precio calculable — sin
        timeout, sin on-chain, sin DexScreener. Si la conexión se corta
        antes de eso (error de red, etc.), devuelve None."""
        print("Esperando el primer trade en vivo del feed de PumpPortal (subscribe_trade) "
              "para fijar el precio de entrada. Esto puede tardar si el token tiene poco volumen.")
        sin_precio = 0
        try:
            async for event in self._trade_events:
                price = self.client.extract_price(event)
                if price is not None and price > 0:
                    print(f"Precio inicial (feed en vivo, subscribe_trade): {price:.10f} SOL/token")
                    return price
                # Llegó un trade pero no se pudo calcular el precio (evento
                # con claves inesperadas). Lo avisamos, con las claves del
                # evento, para poder diagnosticarlo sin quedar en silencio.
                sin_precio += 1
                if sin_precio == 1 or sin_precio % 20 == 0:
                    print(f"[Feed en vivo] llegaron trades pero no se pudo calcular el precio "
                          f"(claves del evento: {sorted(event.keys())}). Sigo esperando...")
            print("[Feed en vivo] la conexión se cerró antes de recibir un trade con precio.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Feed en vivo] la conexión falló: {e}")

        return None

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
            print(f"[Feed de trades de PumpPortal] conexión interrumpida: {e}")

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
                print(f"⏱️  [armado] precio {self.latest_price:.10f} | máximo {pos.highest_price:.10f} "
                      f"| nivel de venta {stop_price:.10f} | PnL: {pnl:+.2f}%")
            else:
                print(f"⏱️  [esperando activación +{self.cfg.activation_pct}%] "
                      f"precio {self.latest_price:.10f} | entrada {pos.entry_price:.10f} | PnL: {pnl:+.2f}%")

    def _on_first_price(self, price: float) -> None:
        self.position = self.executor.buy(self.mint, price)
        print(f"Activación del trailing-stop: +{self.cfg.activation_pct}% "
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
                print(f"✅ Trailing-stop ARMADO. Precio actual {price:.10f} "
                      f"(PnL {pnl:+.2f}%). Máximo inicial registrado.")
            elif price <= pos.entry_price * (1 - self.cfg.initial_stop_pct / 100.0):
                self.executor.sell(pos, price, "stop-loss inicial (nunca se activó el trailing)")
                self._closed_event.set()
            return

        # --- Caso 2: trailing-stop armado, sigue el máximo ----------------- #
        if price > pos.highest_price:
            pos.highest_price = price
            stop_price = pos.highest_price * (1 - self.cfg.trailing_pct / 100.0)
            print(f"📈 Nuevo máximo: {price:.10f} (PnL {pnl:+.2f}%) "
                  f"| nuevo nivel de venta (trailing): {stop_price:.10f}")
            return

        stop_price = pos.highest_price * (1 - self.cfg.trailing_pct / 100.0)
        if price <= stop_price:
            self.executor.sell(
                pos, price,
                f"retroceso de {self.cfg.trailing_pct}% desde el máximo ({pos.highest_price:.10f})"
            )
            self._closed_event.set()
