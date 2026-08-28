#!/usr/bin/env python3
"""
pump_trailing_bot.py
---------------------
Bot de "trailing take-profit" para tokens de pump.fun usando la
Lightning Transaction API de PumpPortal. Toda la configuración se carga
desde un archivo .toml (ver config.toml de ejemplo).

Organización del código:
    - PumpPortalClient: se encarga ÚNICAMENTE de las peticiones (websocket
      de datos y Lightning Trading API). Con la Lightning API, PumpPortal
      firma y envía la transacción en su propio servidor: nosotros solo
      mandamos la orden con nuestra API key, no necesitamos manejar la
      clave privada localmente ni una librería de Solana.
    - TradeExecutor: decide y ejecuta compras/ventas, ya sea SIMULADAS o
      REALES, apoyándose en un PumpPortalClient. Mantiene el estado de la
      posición abierta (Position).
    - TrailingTakeProfitBot: orquesta todo. Escucha el feed de precios del
      PumpPortalClient y aplica la lógica de trailing take-profit, avisando
      al TradeExecutor cuándo comprar y cuándo vender.
    - AppConfig / load_config: una única clase con TODAS las opciones
      (mint, trade, estrategia, PumpPortal), leídas desde un .toml
      organizado en secciones solo por legibilidad.

Flujo de la estrategia:
  1. En el primer precio recibido, se compra (simulada o real).
  2. Mientras el precio NO haya subido `activation_pct` % desde la entrada,
     solo se vigila un stop-loss "duro" (`initial_stop_pct`) por si el
     precio se desploma antes de arrancar.
  3. Una vez que el precio sube `activation_pct` % desde la entrada, el
     trailing-stop se "arma": se registra el máximo precio alcanzado, y el
     nivel de venta va subiendo junto con ese máximo, siempre
     `trailing_pct` % por debajo de él.
  4. Si el precio retrocede hasta tocar ese nivel, se vende.

Por defecto todo es SIMULADO (`general.live = false` en el toml). Para
operar en real hay que poner `live = true` y configurar una API key de
PumpPortal (`pumpportal.api_key`, o la variable de entorno
PUMPPORTAL_API_KEY) asociada a una wallet ya fondeada en PumpPortal.

⚠️ El precio SIEMPRE sale del feed en vivo de PumpPortal (subscribeTokenTrade),
nunca de on-chain ni de DexScreener: no hay otra fuente. Eso significa que
`pumpportal.api_key` es OBLIGATORIA siempre (aunque live = false), porque
subscribeTokenTrade es un stream medido (0.01 SOL cada 10.000 eventos) y
requiere API key + wallet con al menos 0.02 SOL. Sin eso, el bot se queda
esperando para siempre.

⚠️ Esto es una herramienta de trading, no un consejo financiero. Las memecoins
de pump.fun son extremadamente volátiles y de altísimo riesgo. Probá siempre
primero en modo simulado y con montos chicos.

Requisitos:
    pip install websockets requests
    # Python 3.11+ trae tomllib incluido. En versiones anteriores:
    pip install tomli

Uso:
    python pump_trailing_bot.py --config config.toml
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field, fields
from typing import AsyncIterator, Optional

import requests

try:
    import websockets
except ImportError:
    print("Falta la librería 'websockets'. Instalala con: pip install websockets")
    sys.exit(1)

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        print("Falta la librería 'tomli' (necesaria en Python < 3.11). Instalala con: pip install tomli")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Configuración (cargada desde .toml)
# --------------------------------------------------------------------------- #

@dataclass
class AppConfig:
    # --- [general] --------------------------------------------------- #
    mint: str = ""
    live: bool = False
    # Cada cuánto tiempo (segundos) se muestra en pantalla el %% de profit actual.
    status_interval_seconds: float = 5.0

    # --- [trade] ------------------------------------------------------- #
    buy_sol: float = 0.05
    slippage: float = 15.0
    priority_fee: float = 0.00001
    pool: str = "auto"  # "pump", "raydium", "pump-amm", "launchlab", "raydium-cpmm", "bonk", "auto"

    # --- [strategy] ------------------------------------------------------ #
    activation_pct: float = 10.0
    trailing_pct: float = 15.0
    initial_stop_pct: float = 25.0

    # --- [pumpportal] ----------------------------------------------------- #
    api_key: str = ""  # también se puede definir con la variable de entorno PUMPPORTAL_API_KEY


def load_config(path: str) -> AppConfig:
    """Lee el .toml (organizado en secciones [general]/[trade]/[strategy]/
    [pumpportal] solo por legibilidad) y arma UNA única AppConfig con todos
    los campos juntos."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    valid_keys = {f.name for f in fields(AppConfig)}
    merged: dict = {}
    for section_name in ("general", "trade", "strategy", "pumpportal"):
        section = raw.get(section_name, {})
        unknown = set(section) - valid_keys
        if unknown:
            print(f"Aviso: claves desconocidas en el .toml para [{section_name}]: {sorted(unknown)}")
        merged.update({k: v for k, v in section.items() if k in valid_keys})

    config = AppConfig(**merged)

    if not config.api_key:
        config.api_key = os.environ.get("PUMPPORTAL_API_KEY", "")

    if not config.mint:
        raise ValueError("Falta 'mint' en la sección [general] del archivo .toml")

    if not config.api_key:
        raise ValueError(
            "Falta la API key de PumpPortal (pumpportal.api_key en el .toml, o la variable de "
            "entorno PUMPPORTAL_API_KEY). Es obligatoria SIEMPRE, aunque general.live = false: "
            "el bot usa únicamente subscribeTokenTrade para el precio, y ese feed requiere API "
            "key + wallet con al menos 0.02 SOL para entregar trades."
        )

    return config


# --------------------------------------------------------------------------- #
# Cliente de PumpPortal: solo peticiones (websocket + Lightning Trading API)
# --------------------------------------------------------------------------- #

class PumpPortalClient:
    """
    Encapsula toda la comunicación de red con PumpPortal. No conoce nada
    sobre estrategia de trading: solo sabe pedir datos y mandar órdenes
    a través de la Lightning Transaction API (PumpPortal firma y envía la
    transacción por su cuenta; nosotros solo necesitamos la API key).
    """

    DATA_WS_URL = "wss://pumpportal.fun/api/data"
    LIGHTNING_TRADE_URL = "https://pumpportal.fun/api/trade"

    # Supply estándar de un token creado en pump.fun (1.000.000.000 tokens),
    # usado para derivar el precio a partir de `marketCapSol` en los trades
    # que ya no traen las reservas de la bonding curve (token migrado a
    # PumpSwap). Ver extract_price.
    TOTAL_SUPPLY_TOKENS = 1_000_000_000

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    # ---- Feed de precios (SOLO PumpPortal, subscribeTokenTrade) ---------- #

    async def connect_trade_stream(self, mint: str):
        """
        Abre la conexión al websocket de datos de PumpPortal y hace el
        subscribe_trade (`subscribeTokenTrade`) al `mint` indicado, dejando
        la conexión abierta para que se pueda reutilizar tanto para
        conseguir el precio inicial (antes de comprar) como para el
        monitoreo posterior, sin reconectar ni volver a suscribirse.

        OJO: `subscribeTokenTrade` es un stream medido de PumpPortal (0.01
        SOL cada 10.000 eventos) y requiere una API key vinculada a una
        wallet con al menos 0.02 SOL, sin importar si el bot está en modo
        SIMULADO o REAL. Sin API key, la conexión y la suscripción NO dan
        error, pero tampoco entregan ningún trade: el bot se queda
        esperando para siempre (no hay otra fuente de precio de respaldo).
        """
        url = self.DATA_WS_URL
        if self.api_key:
            url = f"{url}?api-key={self.api_key}"
        else:
            print("⚠️  Sin pumpportal.api_key configurada: subscribeTokenTrade NO va a entregar "
                  "ningún trade (requiere API key + wallet con >= 0.02 SOL, aunque el bot esté en "
                  "modo SIMULADO). Como este bot usa SOLO PumpPortal para el precio, se va a quedar "
                  "esperando para siempre. Configurá pumpportal.api_key o PUMPPORTAL_API_KEY.")

        ws = await websockets.connect(url)
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
        return ws

    @staticmethod
    async def iter_trade_events(ws) -> AsyncIterator[dict]:
        """Va entregando cada evento crudo (dict) que llega por una conexión
        ya abierta y suscripta (ver connect_trade_stream)."""
        async for raw_msg in ws:
            try:
                yield json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

    @classmethod
    def extract_price(cls, event: dict) -> Optional[float]:
        """
        Precio en SOL/token a partir de un evento de subscribeTokenTrade.

        Mientras el token sigue en la bonding curve de pump.fun, el evento
        trae las reservas virtuales (`vSolInBondingCurve`/
        `vTokensInBondingCurve`) y el precio sale de ahí, exacto.

        Si el token ya migró a PumpSwap (típico en tokens de bastante
        volumen), el evento deja de traer esas reservas; en ese caso se usa
        `marketCapSol` -que sí viene en todos los trades, migrados o no-
        junto con el supply estándar de pump.fun para derivar el precio.
        """
        v_sol = event.get("vSolInBondingCurve")
        v_tok = event.get("vTokensInBondingCurve")
        if v_sol is not None and v_tok is not None and v_tok:
            return v_sol / v_tok

        market_cap_sol = event.get("marketCapSol")
        if market_cap_sol is not None:
            try:
                return float(market_cap_sol) / cls.TOTAL_SUPPLY_TOKENS
            except (TypeError, ValueError):
                return None

        return None

    # ---- Trading real (Lightning Transaction API) ------------------------- #

    def execute_lightning_trade(self, action: str, mint: str, amount, denominated_in_sol: bool,
                                 slippage: float, priority_fee: float, pool: str) -> dict:
        """
        Manda la orden a la Lightning API. PumpPortal arma, firma y envía la
        transacción del lado de ellos usando la wallet asociada a la API key.
        Devuelve el JSON de respuesta (incluye la firma de la tx o errores).
        """
        if not self.api_key:
            raise RuntimeError("Se necesita una API key de PumpPortal para operar con la Lightning API.")

        payload = {
            "action": action,  # "buy" o "sell"
            "mint": mint,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "amount": amount,
            "slippage": slippage,
            "priorityFee": priority_fee,
            "pool": pool,
        }

        resp = requests.post(
            f"{self.LIGHTNING_TRADE_URL}?api-key={self.api_key}",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Lightning API devolvió {resp.status_code}: {resp.text}")
        return resp.json()


# --------------------------------------------------------------------------- #
# Posición abierta
# --------------------------------------------------------------------------- #

@dataclass
class Position:
    mint: str
    entry_price: float                 # precio en SOL por token, al comprar
    sol_amount: float                  # SOL invertidos
    token_amount: float                # tokens obtenidos (estimado)
    highest_price: float = field(init=False)
    armed: bool = False                # ¿ya se activó el trailing stop?
    closed: bool = False

    def __post_init__(self):
        self.highest_price = self.entry_price

    def pnl_pct(self, price: float) -> float:
        return (price / self.entry_price - 1.0) * 100.0


# --------------------------------------------------------------------------- #
# Ejecutor de trades: decide y ejecuta compra/venta, simulada o real
# --------------------------------------------------------------------------- #

class TradeExecutor:
    """
    Gestiona la compra y venta de un token, ya sea en modo SIMULADO (no toca
    la red, solo lleva la cuenta en memoria) o en modo REAL (usa un
    PumpPortalClient para mandar la orden a la Lightning API).
    """

    def __init__(self, client: PumpPortalClient, live: bool, config: AppConfig):
        self.client = client
        self.live = live
        self.cfg = config

    def buy(self, mint: str, price: float) -> Position:
        sol_amount = self.cfg.buy_sol

        if self.live:
            try:
                result = self.client.execute_lightning_trade(
                    action="buy", mint=mint, amount=sol_amount, denominated_in_sol=True,
                    slippage=self.cfg.slippage, priority_fee=self.cfg.priority_fee,
                    pool=self.cfg.pool,
                )
                print(f"[REAL] Compra enviada. Respuesta: {result}")
            except Exception as e:
                print(f"[REAL] Error al comprar: {e}")
                raise

        token_amount = sol_amount / price if price > 0 else 0.0
        etiqueta = "REAL" if self.live else "SIMULADO"
        print(f"[{etiqueta}] COMPRA de {sol_amount} SOL en {mint} a precio {price:.10f} SOL/token "
              f"(~{token_amount:,.2f} tokens)")
        return Position(mint=mint, entry_price=price, sol_amount=sol_amount, token_amount=token_amount)

    def sell(self, position: Position, price: float, reason: str) -> None:
        if self.live:
            try:
                result = self.client.execute_lightning_trade(
                    action="sell", mint=position.mint, amount="100%", denominated_in_sol=False,
                    slippage=self.cfg.slippage, priority_fee=self.cfg.priority_fee,
                    pool=self.cfg.pool,
                )
                print(f"[REAL] Venta enviada. Respuesta: {result}")
            except Exception as e:
                print(f"[REAL] Error al vender: {e}")

        pnl = position.pnl_pct(price)
        proceeds = position.token_amount * price
        etiqueta = "REAL" if self.live else "SIMULADO"
        print(f"[{etiqueta}] VENTA de {position.mint} a precio {price:.10f} SOL/token "
              f"| motivo: {reason} | PnL: {pnl:+.2f}% | SOL recibidos (aprox): {proceeds:.6f}")
        position.closed = True


# --------------------------------------------------------------------------- #
# Estrategia: trailing take-profit
# --------------------------------------------------------------------------- #

class TrailingTakeProfitBot:
    """
    Orquesta todo: escucha el feed de precios vía PumpPortalClient y decide,
    a través de un TradeExecutor, cuándo comprar y cuándo vender según la
    lógica de trailing take-profit / stop-loss inicial.
    """

    def __init__(self, client: PumpPortalClient, executor: TradeExecutor, config: AppConfig):
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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args():
    p = argparse.ArgumentParser(description="Bot de trailing take-profit para pump.fun (Lightning API de PumpPortal)")
    p.add_argument("--config", default="config.toml", help="Ruta al archivo .toml de configuración (default: config.toml)")
    return p.parse_args()


def main():
    args = parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"ERROR: no se encontró el archivo de configuración '{args.config}'.")
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR de configuración: {e}")
        sys.exit(1)

    if config.live:
        print("⚠️  MODO REAL ACTIVADO (general.live = true en el .toml). Vas a operar con SOL real.")
        print("    Presioná Ctrl+C ahora si no es lo que querés.")
        time.sleep(3)

    client = PumpPortalClient(api_key=config.api_key)
    executor = TradeExecutor(client=client, live=config.live, config=config)
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=config)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")


if __name__ == "__main__":
    main()