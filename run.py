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
    python run.py --config config.toml
"""

import argparse
import asyncio
import logging
import sys
import time

from pepump.config import load_config
from pepump.pump import PumpPortalClient
from pepump.executor import TradeExecutor
from pepump.bot import TrailingTakeProfitBot
from pepump.logging_config import setup_logging

logger = logging.getLogger()


# Opciones
def parse_args():
    p = argparse.ArgumentParser(description="Bot de trailing take-profit para pump.fun (Lightning API de PumpPortal)")

    p.add_argument("-m", "--mint", type=str, required=True, help="Mint con el que se va a operar")
    p.add_argument("-c", "--config", default="config.toml", help="Ruta al archivo .toml de configuración (default: config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="Logging en nivel DEBUG en vez de INFO")

    return p.parse_args()

def show_header(live):
    print("="*50)
    print(f"\t PePump | Modo {"REAL" if live else "SIMULADO"}")
    if live:
        logger.warning("⚠️  MODO REAL ACTIVADO (general.live = true en el .toml). Vas a operar con SOL real.")
        logger.warning("    Presiona Ctrl+C ahora para detenerlo.")
        time.sleep(3)
    print("="*50)
    
    
# logica inicial
if __name__ == "__main__":
    args = parse_args()
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)

    
    try:
        config = load_config(args.config)
        # Un espacio/tab/salto de línea colado al copiar el mint no rompe
        # el parseo de argparse, pero hace que subscribeTokenTrade nunca
        # matchee ningún trade real -> el bot se queda esperando para
        # siempre sin ningún error visible. Lo limpiamos acá.
        config.mint = args.mint.strip()
    except FileNotFoundError:
        logger.error(f"No se encontró el archivo de configuración '{args.config}'.")
        sys.exit(1)
    except ValueError as e:
        # tomllib.TOMLDecodeError también hereda de ValueError, así que un
        # .toml mal formado cae acá también (probado: da un mensaje claro).
        logger.error(f"ERROR de configuración: {e}")
        sys.exit(1)
   
    show_header(config.live)
    
    client = PumpPortalClient(api_key=config.api_key)
    executor = TradeExecutor(client=client, live=config.live, config=config)
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=config)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Interrumpido por el usuario.")
