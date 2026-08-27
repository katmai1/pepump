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
    - AppConfig / load_config: leen todas las opciones desde un .toml.

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
import base64
import json
import os
import struct
import sys
import time
from dataclasses import dataclass, field, fields
from typing import Any, AsyncIterator, Optional

import requests

try:
    from solders.pubkey import Pubkey as _SoldersPubkey
    _SOLDERS_AVAILABLE = True
except ImportError:
    _SOLDERS_AVAILABLE = False

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
class GeneralConfig:
    mint: str = ""
    live: bool = False
    # Cada cuánto tiempo (segundos) se muestra en pantalla el %% de profit actual.
    status_interval_seconds: float = 5.0
    # Cada cuánto tiempo (segundos) se refresca el precio vía DexScreener,
    # independientemente de si llegan o no trades nuevos por el websocket de
    # PumpPortal (útil para tokens de bajo volumen).
    price_poll_interval_seconds: float = 3.0
    # RPC de Solana usado SOLO para leer (no firmar nada) el precio directo
    # desde la cuenta on-chain de la bonding curve de pump.fun: es la fuente
    # más fiable y rápida posible. Un RPC pago/dedicado responde más rápido
    # y con menos rate-limit que el público por defecto.
    rpc_endpoint: str = "https://api.mainnet-beta.solana.com"


@dataclass
class TradeConfig:
    buy_sol: float = 0.05
    slippage: float = 15.0
    priority_fee: float = 0.00001
    pool: str = "auto"  # "pump", "raydium", "pump-amm", "launchlab", "raydium-cpmm", "bonk", "auto"


@dataclass
class StrategyConfig:
    activation_pct: float = 10.0
    trailing_pct: float = 15.0
    initial_stop_pct: float = 25.0


@dataclass
class PumpPortalConfig:
    api_key: str = ""  # también se puede definir con la variable de entorno PUMPPORTAL_API_KEY


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    trade: TradeConfig = field(default_factory=TradeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    pumpportal: PumpPortalConfig = field(default_factory=PumpPortalConfig)


def _build_section(cls, raw: dict) -> Any:
    """Construye una dataclass de config solo con las claves que reconoce,
    avisando si el .toml trae claves desconocidas (typos, etc)."""
    valid_keys = {f.name for f in fields(cls)}
    unknown = set(raw) - valid_keys
    if unknown:
        print(f"Aviso: claves desconocidas en el .toml para [{cls.__name__}]: {sorted(unknown)}")
    return cls(**{k: v for k, v in raw.items() if k in valid_keys})


def load_config(path: str) -> AppConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    config = AppConfig(
        general=_build_section(GeneralConfig, raw.get("general", {})),
        trade=_build_section(TradeConfig, raw.get("trade", {})),
        strategy=_build_section(StrategyConfig, raw.get("strategy", {})),
        pumpportal=_build_section(PumpPortalConfig, raw.get("pumpportal", {})),
    )

    if not config.pumpportal.api_key:
        config.pumpportal.api_key = os.environ.get("PUMPPORTAL_API_KEY", "")

    if not config.general.mint:
        raise ValueError("Falta 'mint' en la sección [general] del archivo .toml")

    if config.general.live and not config.pumpportal.api_key:
        raise ValueError(
            "general.live = true requiere una API key de PumpPortal "
            "(pumpportal.api_key en el .toml, o la variable de entorno PUMPPORTAL_API_KEY)"
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
    # Fuente de precio auxiliar (no es de PumpPortal): se usa para conseguir
    # un precio ni bien arranca el bot y para refrescarlo periódicamente,
    # sin depender de que justo haya un trade nuevo en el feed de PumpPortal.
    DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"

    # Programa on-chain de pump.fun (igual en Mainnet y Devnet) y decimales
    # estándar usados por los tokens creados en la plataforma.
    PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    SOL_DECIMALS = 9
    TOKEN_DECIMALS = 6

    def __init__(self, api_key: str = "", rpc_endpoint: str = "https://api.mainnet-beta.solana.com"):
        self.api_key = api_key
        self.rpc_endpoint = rpc_endpoint

    # ---- Feed de precios ------------------------------------------------- #

    async def stream_token_trades(self, mint: str) -> AsyncIterator[dict]:
        """
        Se conecta al websocket de datos de PumpPortal, se suscribe a los
        trades del `mint` indicado, y va entregando cada evento crudo (dict).
        """
        url = self.DATA_WS_URL
        if self.api_key:
            url = f"{url}?api-key={self.api_key}"

        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
            async for raw_msg in ws:
                try:
                    yield json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

    @staticmethod
    def extract_price(event: dict) -> Optional[float]:
        """Precio en SOL/token a partir de las reservas de la bonding curve."""
        v_sol = event.get("vSolInBondingCurve")
        v_tok = event.get("vTokensInBondingCurve")
        if v_sol is None or v_tok is None or v_tok == 0:
            return None
        return v_sol / v_tok

    def fetch_price_onchain(self, mint: str) -> Optional[float]:
        """
        Lee DIRECTAMENTE de la blockchain de Solana la cuenta de la bonding
        curve del token —la misma fuente de verdad que usan pump.fun y
        PumpPortal internamente— y calcula el precio actual en SOL/token.
        Es la fuente más fiable y rápida disponible: no depende de que haya
        ocurrido un trade recientemente, ni de que un indexador externo
        (DexScreener) ya haya procesado la última operación.

        Requiere 'solders' instalado (pip install solders) solo para derivar
        la dirección de la cuenta (PDA); no firma ni maneja ninguna clave.
        Si el token ya migró a PumpSwap (bonding curve completa/cerrada),
        devuelve None (en ese caso conviene usar fetch_price_dexscreener).
        """
        if not _SOLDERS_AVAILABLE:
            return None

        try:
            mint_pubkey = _SoldersPubkey.from_string(mint)
            program_pubkey = _SoldersPubkey.from_string(self.PUMP_PROGRAM_ID)
            bonding_curve_pda, _bump = _SoldersPubkey.find_program_address(
                [b"bonding-curve", bytes(mint_pubkey)], program_pubkey,
            )
        except Exception as e:
            print(f"[On-chain] No se pudo derivar la cuenta de la bonding curve: {e}")
            return None

        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [str(bonding_curve_pda), {"encoding": "base64"}],
        }
        try:
            resp = requests.post(self.rpc_endpoint, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json().get("result")
        except Exception as e:
            print(f"[On-chain] Error consultando el RPC de Solana: {e}")
            return None

        value = result.get("value") if result else None
        if not value:
            return None  # cuenta inexistente, o bonding curve ya migrada/cerrada

        try:
            raw = base64.b64decode(value["data"][0])
            # Layout de la cuenta: 8 bytes de discriminador + 5 x u64 (LE) + 1 bool
            (virtual_token_reserves, virtual_sol_reserves,
             _real_token_reserves, _real_sol_reserves,
             _token_total_supply, complete) = struct.unpack_from("<QQQQQ?", raw, 8)
        except Exception as e:
            print(f"[On-chain] No se pudo decodificar la cuenta de la bonding curve: {e}")
            return None

        if complete or virtual_token_reserves <= 0:
            return None  # ya migró a PumpSwap: acá conviene la fuente DexScreener

        sol_amount = virtual_sol_reserves / (10 ** self.SOL_DECIMALS)
        token_amount = virtual_token_reserves / (10 ** self.TOKEN_DECIMALS)
        return sol_amount / token_amount if token_amount > 0 else None

    def fetch_price_dexscreener(self, mint: str) -> Optional[float]:
        """
        Consulta el precio actual del token (en SOL) vía la API pública de
        DexScreener. No requiere API key. Se usa como respaldo/refresco de
        precio para no depender pura y exclusivamente de que ocurran trades
        nuevos en el feed de PumpPortal en ese preciso momento (tokens con
        poco volumen pueden pasar minutos sin ningún trade).

        Nota: tokens recién creados pueden tardar unos segundos en aparecer
        indexados en DexScreener.
        """
        url = self.DEXSCREENER_TOKENS_URL.format(mint=mint)
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[DexScreener] Error al consultar precio: {e}")
            return None

        pairs = data.get("pairs") or []
        if not pairs:
            return None

        # Preferimos pares cotizados en SOL/WSOL (que es la unidad que usa
        # todo el resto del bot) y, entre esos, el de mayor liquidez.
        sol_pairs = [p for p in pairs if (p.get("quoteToken") or {}).get("symbol") in ("SOL", "WSOL")]
        candidates = sol_pairs or pairs

        def liquidity_usd(p: dict) -> float:
            return (p.get("liquidity") or {}).get("usd") or 0.0

        best = max(candidates, key=liquidity_usd)
        try:
            return float(best["priceNative"])
        except (KeyError, TypeError, ValueError):
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

    def __init__(self, client: PumpPortalClient, live: bool, trade_cfg: TradeConfig):
        self.client = client
        self.live = live
        self.trade_cfg = trade_cfg

    def buy(self, mint: str, price: float) -> Position:
        sol_amount = self.trade_cfg.buy_sol

        if self.live:
            try:
                result = self.client.execute_lightning_trade(
                    action="buy", mint=mint, amount=sol_amount, denominated_in_sol=True,
                    slippage=self.trade_cfg.slippage, priority_fee=self.trade_cfg.priority_fee,
                    pool=self.trade_cfg.pool,
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
                    slippage=self.trade_cfg.slippage, priority_fee=self.trade_cfg.priority_fee,
                    pool=self.trade_cfg.pool,
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

    def __init__(self, client: PumpPortalClient, executor: TradeExecutor,
                 mint: str, strategy_cfg: StrategyConfig,
                 status_interval_seconds: float = 5.0,
                 price_poll_interval_seconds: float = 3.0):
        self.client = client
        self.executor = executor
        self.mint = mint
        self.cfg = strategy_cfg
        self.status_interval_seconds = status_interval_seconds
        self.price_poll_interval_seconds = price_poll_interval_seconds
        self.position: Optional[Position] = None
        self.latest_price: Optional[float] = None
        self._closed_event = asyncio.Event()

    async def run(self) -> None:
        """
        Consigue un precio YA (vía DexScreener) y compra de inmediato, sin
        esperar a que ocurra un trade nuevo del token. A partir de ahí corren
        tres tareas en paralelo:
          - el feed de trades de PumpPortal (websocket), para reaccionar lo
            más rápido posible cuando el token SÍ tiene actividad,
          - un refresco periódico de precio vía DexScreener, para que el bot
            siga funcionando aunque el token tenga poco o ningún volumen,
          - la impresión periódica del %% de profit en pantalla.
        """
        print(f"Siguiendo el token: {self.mint}")

        initial_price = await self._get_initial_price()
        if initial_price is None:
            print("ERROR: no se pudo obtener un precio inicial del token (ni por DexScreener "
                  "ni por el feed de PumpPortal). Verificá la dirección del token.")
            return

        self.latest_price = initial_price
        self._on_first_price(initial_price)

        tasks = [
            asyncio.create_task(self._consume_trade_stream()),
            asyncio.create_task(self._price_poll_loop()),
            asyncio.create_task(self._status_printer_loop()),
        ]

        await self._closed_event.wait()

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        print("Bot finalizado.")

    async def _get_initial_price(self) -> Optional[float]:
        """Intenta conseguir un precio inicial de inmediato, probando en
        orden la fuente más fiable primero: 1) on-chain (bonding curve de
        pump.fun), 2) DexScreener, 3) como último recurso, esperar el
        primer trade del websocket de PumpPortal."""
        price = await asyncio.to_thread(self.client.fetch_price_onchain, self.mint)
        if price:
            print(f"Precio inicial (on-chain, bonding curve de pump.fun): {price:.10f} SOL/token")
            return price

        price = await asyncio.to_thread(self.client.fetch_price_dexscreener, self.mint)
        if price:
            print(f"Precio inicial (DexScreener): {price:.10f} SOL/token")
            return price

        print("No se pudo obtener el precio ni on-chain ni por DexScreener. "
              "Esperando el primer trade del feed de PumpPortal...")
        async for event in self.client.stream_token_trades(self.mint):
            price = self.client.extract_price(event)
            if price is not None and price > 0:
                print(f"Precio inicial (primer trade de PumpPortal): {price:.10f} SOL/token")
                return price
        return None

    async def _consume_trade_stream(self) -> None:
        """Escucha los trades del token en tiempo real vía websocket, para
        reaccionar más rápido cuando hay actividad. Es un canal adicional al
        polling de DexScreener, no el único."""
        try:
            async for event in self.client.stream_token_trades(self.mint):
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

    async def _price_poll_loop(self) -> None:
        """Refresca el precio cada `price_poll_interval_seconds`, probando
        primero la lectura on-chain (más rápida y fiable) y usando
        DexScreener como respaldo, para que el bot siga reaccionando aunque
        el token tenga poco volumen y el websocket de trades esté en
        silencio. NO imprime nada por sí solo (eso lo hace el status
        printer, a su propio intervalo) — solo reevalúa la estrategia."""
        while True:
            await asyncio.sleep(self.price_poll_interval_seconds)
            if self.position is None or self.position.closed:
                return
            price = await asyncio.to_thread(self.client.fetch_price_onchain, self.mint)
            if not price:
                price = await asyncio.to_thread(self.client.fetch_price_dexscreener, self.mint)
            if price and price > 0:
                self.latest_price = price
                self._on_price_update(price)

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

    if config.general.live:
        print("⚠️  MODO REAL ACTIVADO (general.live = true en el .toml). Vas a operar con SOL real.")
        print("    Presioná Ctrl+C ahora si no es lo que querés.")
        time.sleep(3)

    if not _SOLDERS_AVAILABLE:
        print("Aviso: 'solders' no está instalado (pip install solders). El bot va a funcionar igual, "
              "pero sin la lectura on-chain del precio (la más rápida y fiable); usará DexScreener.")

    client = PumpPortalClient(api_key=config.pumpportal.api_key, rpc_endpoint=config.general.rpc_endpoint)
    executor = TradeExecutor(client=client, live=config.general.live, trade_cfg=config.trade)
    bot = TrailingTakeProfitBot(
        client=client, executor=executor, mint=config.general.mint, strategy_cfg=config.strategy,
        status_interval_seconds=config.general.status_interval_seconds,
        price_poll_interval_seconds=config.general.price_poll_interval_seconds,
    )

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")


if __name__ == "__main__":
    main()