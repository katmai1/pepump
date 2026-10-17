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
      mandamos la orden con nuestra API key, no necesitamos gestionar la
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
de pump.fun son extremadamente volátiles y de altísimo riesgo. Prueba siempre
primero en modo simulado y con importes pequeños.

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

from pepump.config import load_config, validate_mint
from pepump.pump import PumpPortalClient, TokenMetadataOnChainClient
from pepump.dashboard import MultiPositionPanel
from pepump.logging_config import setup_logging
from pepump.manager import PositionManager
from pepump.shutdown import run_with_shutdown_signals_for

logger = logging.getLogger(__name__)


# Opciones
def parse_args():
    p = argparse.ArgumentParser(description="Bot de trailing take-profit para pump.fun (Lightning API de PumpPortal)")

    p.add_argument("-m", "--mint", type=str, action="append", default=[],
                   help="Mint con el que abrir una posición. Se puede repetir: -m <MINT1> -m <MINT2>. "
                        "Sin -m, solo se retoman las posiciones que quedaron abiertas")
    p.add_argument("-c", "--config", default="config.toml", help="Ruta al archivo .toml de configuración (default: config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="Logging en nivel DEBUG en vez de INFO")
    p.add_argument("--web", nargs="?", type=int, const=8080, default=None, metavar="PUERTO",
                   help="Arranca el panel web en http://127.0.0.1:PUERTO (default 8080) y deja el bot "
                        "en marcha aunque no haya posiciones: se abren y cierran desde la web")
    p.add_argument("--web-host", default="127.0.0.1",
                   help="Interfaz donde escucha el panel web (default 127.0.0.1: solo esta máquina). "
                        "Con la IP de la red local (o 0.0.0.0) lo ve toda la red: conviene poner "
                        "web_password en el .toml")
    p.add_argument("--sell-on-exit", action="store_true",
                   help="Ctrl+C VENDE todas las posiciones antes de salir. Sin esto, Ctrl+C sale sin "
                        "vender y las posiciones se retoman al volver a arrancar")
    p.add_argument("--no-panel", action="store_true",
                   help="Sin panel fijo al pie: el estado se imprime como una línea de log más cada intervalo")

    return p.parse_args()

def show_header(live, opening_now: bool):
    # Sin print(): todo sale por el mismo logger que el resto del bot, así
    # el banner queda en el archivo/redirección junto a las operaciones.
    # Las comillas del modo van en una variable aparte a propósito: un
    # f-string con comillas dobles anidadas solo compila en Python 3.12+
    # (PEP 701), y este proyecto arranca en 3.11 (tomllib).
    # general.live es el modo POR DEFECTO de las posiciones nuevas: desde la
    # web y ctl.py se elige en cada una. Las retomadas siguen en el suyo.
    modo = "REAL ⚠️" if live else "SIMULADO"
    logger.info("=" * 50)
    logger.info(f"\t PePump | Modo por defecto: {modo}")
    logger.info("=" * 50)
    if live and opening_now:
        logger.warning("⚠️  MODO REAL (general.live = true en el .toml): las posiciones de -m se abren con "
                       "SOL real.")
    elif live:
        logger.warning("⚠️  general.live = true: las posiciones nuevas serán REALES por defecto (se puede "
                       "elegir en cada una).")


# logica inicial
if __name__ == "__main__":
    args = parse_args()
    panel = setup_logging(level=logging.DEBUG if args.verbose else logging.INFO, panel=not args.no_panel,
                          tag_mint=True)

    
    try:
        config = load_config(args.config)
        # Los mints vienen SIEMPRE de la línea de comandos, nunca del .toml.
        # Se normalizan y validan aquí para fallar rápido y con un mensaje
        # que apunte a la causa real -ver validate_mint en config.py.
        mints = [validate_mint(m) for m in args.mint]
    except FileNotFoundError:
        logger.error(f"No se encontró el archivo de configuración '{args.config}'.")
        sys.exit(1)
    except ValueError as e:
        # tomllib.TOMLDecodeError también hereda de ValueError, así que un
        # .toml mal formado cae aquí también (probado: da un mensaje claro).
        logger.error(f"ERROR de configuración: {e}")
        sys.exit(1)
    repetidos = sorted({m for m in mints if mints.count(m) > 1})
    if repetidos:
        logger.error(f"Mint repetido: {', '.join(repetidos)}. Comparten wallet, así que no se pueden "
                     f"abrir dos posiciones del mismo token a la vez.")
        sys.exit(1)

    web_sock = None
    log_buffer = None
    if args.web is not None:
        # Imports aquí: sin --web no hace falta tener FastAPI/uvicorn instalados.
        from pepump.web.logbuffer import LogBuffer
        from pepump.web.server import bind_socket, is_loopback
        # El puerto se abre ANTES de comprar nada: si está ocupado, se sale
        # sin haber tocado ninguna posición.
        try:
            web_sock = bind_socket(args.web_host, args.web)
        except OSError as e:
            logger.error(f"No se pudo abrir el panel web en {args.web_host}:{args.web}: {e}")
            sys.exit(1)
        log_buffer = LogBuffer()
        logging.getLogger().addHandler(log_buffer)

    show_header(config.live, opening_now=bool(mints))
    # Después del banner, para que no quede enterrado arriba.
    if web_sock is not None and not is_loopback(args.web_host):
        if config.web_password:
            logger.warning(f"Panel web abierto a la red ({args.web_host}) con contraseña. Va por HTTP "
                           f"sin cifrar: en una red que no sea de confianza, mejor Tailscale o un túnel SSH.")
        else:
            logger.warning(f"⚠️  Panel web abierto a la red ({args.web_host}) SIN contraseña: cualquiera "
                           f"que llegue a esa dirección puede ver y CERRAR (vender) tus posiciones. "
                           f"Para pedir contraseña: web_password en [web] del .toml, o la variable de "
                           f"entorno PEPUMP_WEB_PASSWORD.")

    client = PumpPortalClient(api_key=config.api_key)

    store = None
    if config.state_db:
        from pepump.store import PositionStore
        try:
            store = PositionStore(config.state_db)
        except Exception as e:
            logger.error(f"No se pudo abrir la base de datos de estado '{config.state_db}': {e}")
            sys.exit(1)
    elif not args.sell_on_exit:
        logger.warning("general.state_db está vacío: sin base de datos, lo que quede abierto al salir "
                       "NO se retoma después. Considera usar --sell-on-exit.")

    async def main():
        manager = PositionManager(base_config=config, client=client, store=store,
                                  metadata_client=TokenMetadataOnChainClient(config.solana_rpc_url))
        if panel is not None:
            multi = MultiPositionPanel(panel, manager.snapshot, live=config.live)
            if args.sell_on_exit:
                multi.ctrl_c_hint = "Ctrl+C vende todas y sale"
            manager.panel_factory = multi.for_position

        web = None
        if web_sock is not None:
            from pepump.web.server import WebPanel, allowed_hosts_for, create_app
            app = create_app(manager, log_buffer, live=config.live,
                             allowed_hosts=allowed_hosts_for(args.web_host),
                             password=config.web_password)
            web = WebPanel(app, web_sock)
            web.start()
            shown_host = "127.0.0.1" if args.web_host in ("0.0.0.0", "::") else args.web_host
            logger.info(f"Panel web: http://{shown_host}:{args.web}")

        # Con --web el proceso es un servicio: sigue vivo sin posiciones
        # (se abren desde la web) hasta que se pide salir.
        salir = asyncio.Event()

        def on_signal(sig_name: str) -> None:
            salir.set()
            if args.sell_on_exit:
                manager.close_all(f"{sig_name} recibido")
            else:
                manager.stop_all(f"{sig_name} recibido, saliendo sin vender")

        async def run_all():
            retomadas = await manager.recover()
            for mint in mints:
                if manager.get(mint) is not None and manager.get(mint).active:
                    logger.info(f"{mint} ya estaba abierta y se retomó: no se abre otra.")
                    continue
                try:
                    manager.open(mint)
                except ValueError as e:
                    logger.error(f"No se abre {mint}: {e}")
            if web is not None:
                logger.info(f"Bot en marcha: abre y cierra posiciones desde el panel web. Ctrl+C "
                            f"sale {'VENDIENDO todo' if args.sell_on_exit else 'sin vender'}.")
                await salir.wait()
                await manager.wait_all()
                return
            if not manager.positions():
                logger.info("No hay posiciones abiertas que retomar ni mints (-m) que abrir. Nada que "
                            "hacer. (Con --web el bot se queda en marcha y las abres desde la web.)")
                return
            if retomadas and not mints:
                logger.info(f"Retomadas {len(retomadas)} posiciones. Ctrl+C sale "
                            f"{'vendiendo' if args.sell_on_exit else 'SIN vender'}.")
            await manager.wait_all()

        try:
            await run_with_shutdown_signals_for(run_all(), on_signal)
        finally:
            if web is not None:
                await web.stop()
            await manager.aclose()
            abiertas = [m for m in manager.positions() if m.bot.detached]
            if abiertas:
                logger.warning(f"⏸️  {len(abiertas)} posición(es) quedan ABIERTAS sin vigilar: "
                               f"{', '.join(f'#{m.id}' for m in abiertas)}. Se retoman al volver a "
                               f"arrancar el bot (python run.py, con o sin -m).")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrumpido por el usuario.")
    finally:
        if panel is not None:
            panel.close()
        if store is not None:
            store.close()
