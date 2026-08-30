from dataclasses import dataclass, fields
import os
import tomllib


@dataclass
class AppConfig:
    # --- [general] --------------------------------------------------- #
    mint: str = ""
    live: bool = False
    # Cada cuánto tiempo (segundos) se muestra en pantalla el %% de profit actual.
    status_interval_seconds: float = 5.0
    # Ruta al .csv donde se va agregando (append) una fila por cada orden
    # CERRADA (venta confirmada, real o simulada) -ver executor.py/history.py.
    # Si la ruta no existe todavía, se crea (junto con carpetas intermedias)
    # al cerrar la primera orden; si ya existe, se sigue agregando abajo sin
    # tocar lo que ya había. "" desactiva el historial.
    trade_history_csv: str = "trade_history.csv"

    # --- [trade] ------------------------------------------------------- #
    buy_sol: float = 0.05
    slippage: float = 15.0
    priority_fee: float = 0.00001
    pool: str = "auto"  # "pump", "raydium", "pump-amm", "launchlab", "raydium-cpmm", "bonk", "auto"

    # --- [strategy] ------------------------------------------------------ #
    activation_pct: float = 10.0
    trailing_pct: float = 15.0
    initial_stop_pct: float = 25.0
    # Baja %% desde el precio de referencia (el primer precio que llega al
    # arrancar) que se espera ANTES de comprar. 0 (default) = comprar de
    # una al precio de referencia, exactamente como antes. Si es > 0
    # (ej. 5), el bot NO compra en la referencia: sigue mirando el precio
    # y recién entra cuando toca `referencia * (1 - entry_dip_pct/100)` o
    # menos (ver _wait_for_dip_entry en bot.py). Si el precio nunca baja
    # tanto, el bot se queda esperando indefinidamente -cancelá con
    # Ctrl+C/SIGTERM si hace falta, no hay timeout para esto.
    entry_dip_pct: float = 0.0

    # --- [pumpportal] ----------------------------------------------------- #
    api_key: str = ""  # también se puede definir con la variable de entorno PUMPPORTAL_API_KEY

    # --- [onchain] ---------------------------------------------------------- #
    # Cuántos segundos esperar el precio por el feed en vivo de PumpPortal
    # (subscribeTokenTrade) ANTES de recurrir al fallback on-chain de
    # PumpSwap. Solo aplica si ya llegó el ack de suscripción pero ningún
    # trade real -> típicamente un mint que ya migró a PumpSwap, caso en
    # el que subscribeTokenTrade no entrega nada (ver pump.py). Mientras
    # el mint sigue en bonding curve, el feed en vivo funciona bien y este
    # timeout no debería llegar a cumplirse casi nunca.
    live_feed_timeout_seconds: float = 5.0
    # Tiempo TOTAL (segundos) que el bot está dispuesto a esperar el precio
    # de entrada antes de abortar del todo (ver _get_reference_price en
    # bot.py). Cubre toda la espera DESPUÉS de recibir el ack de
    # suscripción: ciclos de live_feed_timeout_seconds + intentos de
    # fallback on-chain, uno tras otro, hasta que se cumpla este total.
    # Si se cumple sin haber conseguido precio (ni por el feed en vivo ni
    # on-chain), se aborta la entrada y el bot no compra nada.
    entry_wait_timeout_seconds: float = 60.0
    # RPC de Solana usado ÚNICAMENTE para leer, on-chain, las reservas
    # reales del pool de PumpSwap cuando el mint ya migró (ver
    # pumpswapamm en pump.py). Un endpoint público gratuito alcanza para
    # esto (una sola lectura, no trading), pero es lento/rate-limited;
    # para uso serio conviene un RPC dedicado (Helius, QuickNode, etc.).
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    # Si se entró en un mint por el fallback on-chain (porque
    # subscribeTokenTrade no entregó nada), cada cuántos segundos se
    # vuelve a consultar el precio on-chain mientras la posición sigue
    # abierta, para poder evaluar trailing-stop/stop-loss sin depender
    # del feed en vivo que ya sabemos que no funciona para ese mint.
    onchain_poll_interval_seconds: float = 5.0
    # Igual que live_feed_timeout_seconds pero para una posición YA
    # ABIERTA: si pasan estos segundos sin ningún trade nuevo por
    # subscribeTokenTrade, puede ser que el mint haya migrado a PumpSwap
    # A MITAD de la posición (el socket sigue abierto y no tira ningún
    # error -simplemente deja de mandar trades para ese mint- así que el
    # precio quedaba congelado para siempre sin aviso). Cuando se cumple
    # este timeout, se hace UNA consulta on-chain puntual para confirmar;
    # si hay un pool de PumpSwap con precio válido, el bot pasa a polling
    # on-chain (onchain_poll_interval_seconds) para el resto de la
    # posición. Si no, se asume que es solo una pausa de volumen y se
    # sigue esperando el feed en vivo normalmente. Un valor más alto que
    # live_feed_timeout_seconds evita falsos positivos en tokens con
    # volumen intermitente pero todavía en bonding curve.
    stall_timeout_seconds: float = 20.0
    # Después de mandar una compra/venta REAL (Lightning API), cuántos
    # segundos esperar a que la transacción confirme on-chain antes de
    # darla por fallida. Necesario porque PumpPortal puede devolver un
    # 200 OK con firma de forma "optimista", antes de saber si la tx va
    # a confirmar o reventar (p. ej. por slippage excedido) — ver
    # execute_lightning_trade en pump.py.
    tx_confirm_timeout_seconds: float = 30.0
    tx_confirm_poll_interval_seconds: float = 2.0


def load_config(path: str) -> AppConfig:
    """Lee el .toml (organizado en secciones [general]/[trade]/[strategy]/
    [pumpportal] solo por legibilidad) y arma UNA única AppConfig con todos
    los campos juntos."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    valid_keys = {f.name for f in fields(AppConfig)}
    merged: dict = {}
    for section_name in ("general", "trade", "strategy", "pumpportal", "onchain"):
        section = raw.get(section_name, {})
        unknown = set(section) - valid_keys
        if unknown:
            print(f"Aviso: claves desconocidas en el .toml para [{section_name}]: {sorted(unknown)}")
        merged.update({k: v for k, v in section.items() if k in valid_keys})

    config = AppConfig(**merged)

    # Defensivo: un espacio, tab o salto de línea colado al copiar el mint
    # (o la api_key) al .toml no rompe el parseo del TOML en sí, pero hace
    # que el subscribeTokenTrade se acepte igual (PumpPortal no valida que
    # el mint exista) y después nunca matchee ningún trade real -> el bot
    # se queda esperando para siempre en silencio. Lo limpiamos acá.
    #config.mint = config.mint.strip()

    # BUGFIX: el .toml de ejemplo y los mensajes de error de acá abajo
    # siempre dijeron que la api_key también se podía definir con la
    # variable de entorno PUMPPORTAL_API_KEY (para no tener que escribirla
    # en el archivo), pero nunca se leía realmente -> quien confiara en
    # esa opción se encontraba con "Falta la API key" igual. Si el .toml
    # no trae una key, ahora sí se consulta la variable de entorno como
    # fallback antes de fallar.
    if not config.api_key:
        config.api_key = os.environ.get("PUMPPORTAL_API_KEY", "")
    config.api_key = config.api_key.strip()

    # if not config.mint:
    #     raise ValueError("Falta 'mint' en la sección [general] del archivo .toml")

    if not config.api_key:
        raise ValueError(
            "Falta la API key de PumpPortal (pumpportal.api_key en el .toml, o la variable de "
            "entorno PUMPPORTAL_API_KEY). Es obligatoria SIEMPRE, aunque general.live = false: "
            "el bot usa únicamente subscribeTokenTrade para el precio, y ese feed requiere API "
            "key + wallet con al menos 0.02 SOL para entregar trades."
        )

    return config