import logging
import os
import stat
import tomllib
from dataclasses import dataclass, field, fields

from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    # --- [general] --------------------------------------------------- #
    mint: str = ""
    live: bool = False
    # Modo SIMULADO: cobrar los costes que tendría una operación real
    # (ver executor.simulated_buy / simulated_sell) para que el PnL simulado
    # se parezca al real. False = compra y venta exactas al precio de
    # mercado, sin ningún coste.
    sim_costs: bool = True
    # Comisiones por operación, en % del SOL: pump.fun (~1% en la bonding
    # curve, ~0.3% en PumpSwap) + PumpPortal (1% con la Lightning API).
    sim_fee_pct: float = 2.0
    # Slippage en contra al simular: se compra este % más caro y se vende
    # este % más barato que el precio de mercado.
    sim_slippage_pct: float = 1.0
    # Cada cuánto tiempo (segundos) se muestra en pantalla el %% de profit actual.
    status_interval_seconds: float = 5.0
    # Ruta al .csv donde se va agregando (append) una fila por cada orden
    # CERRADA (venta confirmada, real o simulada) -ver executor.py/history.py.
    # Si la ruta no existe todavía, se crea (junto con carpetas intermedias)
    # al cerrar la primera orden; si ya existe, se sigue agregando abajo sin
    # tocar lo que ya había. "" desactiva el historial.
    trade_history_csv: str = "trade_history.csv"
    # Base de datos SQLite donde se guarda el estado de las posiciones para
    # retomarlas al volver a arrancar (ver store.py). "" la desactiva: al
    # salir, lo que quede abierto se pierde de vista.
    state_db: str = "pepump.db"

    # Máximo de posiciones activas a la vez (contando las retomadas). Abrir
    # una más da error, venga de la web, de ctl.py o de -m.
    max_open_positions: int = 10

    # --- [trade] ------------------------------------------------------- #
    buy_sol: float = 0.05
    # Tope de SOL por posición: red de seguridad contra un error de dedo al
    # abrir desde la web (10 en vez de 0.10). Ni el .toml ni la web pueden
    # pedir una compra mayor. Solo se cambia editando el .toml.
    max_buy_sol: float = 0.5
    slippage: float = 15.0
    priority_fee: float = 0.00001
    pool: str = "auto"  # ver POOLS_VALIDOS
    # Topes para los REINTENTOS de una venta fallida (ver
    # executor.sell_retry_params): cada fallo sube el slippage y el priority
    # fee, porque cuando salta un stop el precio suele estar cayendo rápido
    # y repetir la misma orden falla igual. Nunca bajan de slippage /
    # priority_fee.
    max_sell_slippage: float = 50.0
    max_sell_priority_fee: float = 0.001

    # --- [strategy] ------------------------------------------------------ #
    activation_pct: float = 10.0
    trailing_pct: float = 15.0
    initial_stop_pct: float = 25.0
    # Baja %% desde el precio de referencia (el primer precio que llega al
    # arrancar) que se espera ANTES de comprar. 0 (default) = comprar de
    # una al precio de referencia, exactamente como antes. Si es > 0
    # (ej. 5), el bot NO compra en la referencia: sigue mirando el precio
    # y no entra hasta que toca `referencia * (1 - entry_dip_pct/100)` o
    # menos (ver _wait_for_dip_entry en bot.py). Si el precio nunca baja
    # tanto, el bot se queda esperando indefinidamente -cancela con
    # Ctrl+C/SIGTERM si hace falta, no hay timeout para esto.
    entry_dip_pct: float = 0.0
    # Contra qué precio de entrada se miden activation_pct e
    # initial_stop_pct (ver ESTRATEGIA_BASES):
    #   "real"   -> el precio EFECTIVO pagado (con comisiones y priority
    #               fee, sin el rent de la cuenta de token, que vuelve al
    #               vender), leído de la tx de compra confirmada. Es lo que
    #               de verdad ganas o pierdes.
    #   "market" -> el precio de MERCADO al comprar (el que muestra
    #               pump.fun), sin costes. Era el comportamiento anterior.
    # Sin datos de fill (compra real sin poder leer la tx, o simulado con
    # general.sim_costs = false) los dos precios son el mismo y da igual.
    strategy_price_basis: str = "real"

    # --- [pumpportal] ----------------------------------------------------- #
    api_key: str = ""  # también se puede definir con la variable de entorno PUMPPORTAL_API_KEY
    # Dirección PÚBLICA de la wallet asociada a la api_key (opcional). La API
    # de PumpPortal no la da: sin esto, el bot solo la conoce tras la primera
    # compra real. Con ella, la web muestra el saldo desde el arranque y se
    # comprueba que haya SOL antes de cada compra real.
    wallet_address: str = ""

    # --- [jupiter] ---------------------------------------------------------- #
    # API key de Jupiter (portal de desarrolladores). Con ella, los tokens
    # que no cotizan contra SOL en pump.fun, PumpSwap, Raydium CPMM ni
    # Meteora DLMM se operan por Jupiter (ver jupiter.py). Vacía = sin
    # Jupiter. También con la variable de entorno JUPITER_API_KEY. Para
    # operar en REAL hace falta además la clave privada de la wallet
    # (wallet_private_key, abajo).
    jupiter_api_key: str = ""
    # Clave PRIVADA de la wallet (la misma que la de PumpPortal), para firmar
    # las órdenes de Jupiter. Vacía = Jupiter solo en simulado. También con
    # la variable de entorno PEPUMP_WALLET_PRIVATE_KEY. repr=False: que no
    # salga nunca en un log ni en un traceback que muestre la config.
    wallet_private_key: str = field(default="", repr=False)
    # Mínimo de segundos entre dos peticiones a Jupiter, para TODA la
    # cuenta (precio de todas las posiciones + órdenes). 2.0 es el plan
    # gratuito; con un plan de pago se puede bajar.
    jupiter_request_interval_seconds: float = 2.0
    # True = TODAS las órdenes van por Jupiter (también las de pump.fun y
    # PumpSwap), no solo las de tokens que PumpPortal no sabe operar. El
    # precio sigue saliendo de donde siempre. En REAL hace falta
    # wallet_private_key; sin ella, siguen yendo por PumpPortal.
    jupiter_for_all: bool = False

    # --- [web] -------------------------------------------------------------- #
    # Contraseña del panel web (--web). Opcional: vacía, el panel no pide
    # nada. También se puede definir con la variable de entorno
    # PEPUMP_WEB_PASSWORD.
    web_password: str = ""

    # --- [onchain] ---------------------------------------------------------- #
    # Cuántos segundos esperar el precio por el feed en vivo de PumpPortal
    # (subscribeTokenTrade) ANTES de recurrir al fallback on-chain de
    # PumpSwap. Solo aplica si ya llegó el ack de suscripción pero ningún
    # trade real -> típicamente un mint que ya migró a PumpSwap, caso en
    # el que subscribeTokenTrade no entrega nada (ver pumpportal.py). Mientras
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
    # PumpSwapOnChainClient en onchain/pumpswap.py). Un endpoint público gratuito alcanza para
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
    # A MITAD de la posición (el socket sigue abierto y no lanza ningún
    # error -simplemente deja de mandar trades para ese mint- así que el
    # precio quedaba congelado para siempre sin aviso). Cuando se cumple
    # este timeout, se hace UNA consulta on-chain puntual para confirmar;
    # si hay un pool de PumpSwap con precio válido, el bot pasa a polling
    # on-chain (onchain_poll_interval_seconds) para el resto de la
    # posición. Si no, se asume que es solo una pausa de volumen y se
    # sigue esperando el feed en vivo normalmente. Un valor más alto que
    # live_feed_timeout_seconds evita falsos positivos en tokens con
    # volumen intermitente pero todavía en bonding curve.
    stall_timeout_seconds: float = 10.0
    # Después de mandar una compra/venta REAL (Lightning API), cuántos
    # segundos esperar a que la transacción confirme on-chain antes de
    # darla por fallida. Necesario porque PumpPortal puede devolver un
    # 200 OK con firma de forma "optimista", antes de saber si la tx va
    # a confirmar o reventar (p. ej. por slippage excedido) — ver
    # execute_lightning_trade en pumpportal.py.
    tx_confirm_timeout_seconds: float = 30.0
    tx_confirm_poll_interval_seconds: float = 2.0


# Valores que la Lightning API de PumpPortal acepta en el campo `pool`.
POOLS_VALIDOS = ("pump", "raydium", "pump-amm", "launchlab", "raydium-cpmm", "bonk", "auto")
# Valores válidos de strategy.strategy_price_basis.
ESTRATEGIA_BASES = ("real", "market")


def validate_mint(mint: str) -> str:
    """Normaliza y valida una dirección de mint, devolviéndola limpia.

    Un espacio, tab o salto de línea colado al copiar el mint no rompe el
    parseo de argparse ni hace fallar el subscribeTokenTrade (PumpPortal
    acepta la suscripción sin verificar que el mint exista), así que el
    bot arrancaba de todos modos y después nunca coincidía con ningún
    trade. Y un mint directamente inválido tampoco se detectaba aquí:
    reventaba mucho más
    tarde, dentro del fallback on-chain, donde el `except` amplio lo
    convertía en "no hay ninguna fuente de precio disponible" -un mensaje
    que no tiene nada que ver con la causa real.

    Lanza ValueError con un mensaje claro si no es una pubkey válida de
    Solana (base58, 32 bytes)."""
    limpio = (mint or "").strip()
    if not limpio:
        raise ValueError("El mint está vacío.")
    try:
        Pubkey.from_string(limpio)
    except Exception:
        raise ValueError(
            f"'{limpio}' no es una dirección de mint válida de Solana (se espera una pubkey "
            f"base58 de 32 bytes, típicamente 43-44 caracteres). Revisa que lo hayas copiado "
            f"entero y sin caracteres de más."
        ) from None
    return limpio


def validate_config(config: AppConfig) -> None:
    """Comprobaciones de coherencia de la configuración, ANTES de conectarse a
    nada. Todo lo que se valide aquí es un error que, si no, aparecería
    mucho más tarde y disfrazado de otra cosa: un `buy_sol` en 0 se manda
    de todos modos a la Lightning API y vuelve como un rechazo genérico; un
    `entry_dip_pct` de 100 deja el precio objetivo en 0 y el bot esperando
    para siempre sin ninguna señal de que nunca va a entrar; un
    `trailing_pct` de 100 pone el nivel de venta en 0 y desarma el
    trailing-stop sin avisar.

    Los errores duros levantan ValueError (run.py lo muestra y sale con
    código 1). Lo que es raro pero puede ser deliberado se avisa por log y
    el bot arranca de todos modos."""
    errores = []

    def positivo(nombre, valor, incluir_cero=False):
        if valor is None or (valor < 0 if incluir_cero else valor <= 0):
            limite = ">= 0" if incluir_cero else "> 0"
            errores.append(f"{nombre} tiene que ser {limite} (está en {valor})")

    def porcentaje_abierto(nombre, valor, minimo_incluido=False):
        """Porcentaje en (0, 100) -o [0, 100) si minimo_incluido."""
        if valor is None or valor < 0 or valor >= 100 or (valor == 0 and not minimo_incluido):
            rango = "[0, 100)" if minimo_incluido else "(0, 100)"
            errores.append(f"{nombre} tiene que estar en el rango {rango} (está en {valor})")

    porcentaje_abierto("general.sim_fee_pct", config.sim_fee_pct, minimo_incluido=True)
    porcentaje_abierto("general.sim_slippage_pct", config.sim_slippage_pct, minimo_incluido=True)
    positivo("trade.buy_sol", config.buy_sol)
    positivo("trade.max_buy_sol", config.max_buy_sol)
    if config.buy_sol and config.max_buy_sol and config.buy_sol > config.max_buy_sol:
        errores.append(f"trade.buy_sol ({config.buy_sol} SOL) supera el máximo por posición "
                       f"trade.max_buy_sol ({config.max_buy_sol} SOL)")
    if not isinstance(config.max_open_positions, int) or config.max_open_positions < 1:
        errores.append(f"general.max_open_positions tiene que ser un entero >= 1 "
                       f"(está en {config.max_open_positions})")
    positivo("trade.slippage", config.slippage)
    positivo("trade.priority_fee", config.priority_fee, incluir_cero=True)
    if config.max_sell_slippage is None or not 0 < config.max_sell_slippage <= 100:
        errores.append(f"trade.max_sell_slippage tiene que estar en el rango (0, 100] "
                       f"(está en {config.max_sell_slippage})")
    positivo("trade.max_sell_priority_fee", config.max_sell_priority_fee, incluir_cero=True)
    if config.pool not in POOLS_VALIDOS:
        errores.append(f"trade.pool tiene que ser uno de {list(POOLS_VALIDOS)} (está en {config.pool!r})")

    positivo("strategy.activation_pct", config.activation_pct)
    porcentaje_abierto("strategy.trailing_pct", config.trailing_pct)
    porcentaje_abierto("strategy.initial_stop_pct", config.initial_stop_pct)
    porcentaje_abierto("strategy.entry_dip_pct", config.entry_dip_pct, minimo_incluido=True)
    if config.strategy_price_basis not in ESTRATEGIA_BASES:
        errores.append(f"strategy.strategy_price_basis tiene que ser uno de {list(ESTRATEGIA_BASES)} "
                       f"(está en {config.strategy_price_basis!r})")

    positivo("general.status_interval_seconds", config.status_interval_seconds)
    positivo("onchain.live_feed_timeout_seconds", config.live_feed_timeout_seconds)
    positivo("onchain.entry_wait_timeout_seconds", config.entry_wait_timeout_seconds)
    positivo("onchain.onchain_poll_interval_seconds", config.onchain_poll_interval_seconds)
    positivo("onchain.stall_timeout_seconds", config.stall_timeout_seconds)
    positivo("onchain.tx_confirm_timeout_seconds", config.tx_confirm_timeout_seconds)
    positivo("onchain.tx_confirm_poll_interval_seconds", config.tx_confirm_poll_interval_seconds)
    positivo("jupiter.jupiter_request_interval_seconds", config.jupiter_request_interval_seconds)

    if config.wallet_address:
        try:
            validate_mint(config.wallet_address)  # misma validación: pubkey base58
        except ValueError:
            errores.append(f"pumpportal.wallet_address no es una dirección válida de Solana "
                           f"({config.wallet_address!r})")

    if not str(config.solana_rpc_url).startswith(("http://", "https://")):
        errores.append(f"onchain.solana_rpc_url tiene que ser una URL http(s) "
                       f"(está en {config.solana_rpc_url!r})")

    if config.entry_wait_timeout_seconds < config.live_feed_timeout_seconds:
        errores.append(
            f"onchain.entry_wait_timeout_seconds ({config.entry_wait_timeout_seconds}) no puede ser "
            f"menor que live_feed_timeout_seconds ({config.live_feed_timeout_seconds}): el primero "
            f"es el presupuesto TOTAL de espera del precio de entrada, y el segundo un ciclo de esa "
            f"espera")

    if config.tx_confirm_poll_interval_seconds > config.tx_confirm_timeout_seconds:
        errores.append(
            f"onchain.tx_confirm_poll_interval_seconds ({config.tx_confirm_poll_interval_seconds}) no "
            f"puede ser mayor que tx_confirm_timeout_seconds ({config.tx_confirm_timeout_seconds}): "
            f"no llegaría a consultar el estado ni una vez antes de darla por fallida")

    if errores:
        raise ValueError("Configuración inválida:\n  - " + "\n  - ".join(errores))

    # --- Avisos: raro pero puede ser a propósito ------------------------- #
    if config.stall_timeout_seconds <= config.live_feed_timeout_seconds:
        logger.warning(
            f"onchain.stall_timeout_seconds ({config.stall_timeout_seconds}s) es <= "
            f"live_feed_timeout_seconds ({config.live_feed_timeout_seconds}s). Con la posición ya "
            f"abierta eso dispara la confirmación on-chain ante cualquier pausa corta de volumen, "
            f"gastando consultas de RPC de más.")
    if config.slippage > 50:
        logger.warning(f"trade.slippage está en {config.slippage}%: con memecoins eso puede hacer que "
                       f"la orden entre a un precio muchísimo peor que el de referencia.")
    if config.live and config.buy_sol >= 1.0:
        logger.warning(f"MODO REAL con trade.buy_sol = {config.buy_sol} SOL por operación. "
                       f"Confirma que sea el importe que quieres arriesgar.")


def _warn_if_readable_by_others(path: str) -> None:
    """Con la clave privada dentro, el .toml solo lo debería poder leer su
    dueño. En Windows los permisos POSIX no dicen nada: no se comprueba."""
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(f"⚠️  {path} tiene la clave privada de la wallet y lo pueden leer otros usuarios "
                       f"de esta máquina. Protégelo con: chmod 600 {path}")


def load_config(path: str) -> AppConfig:
    """Lee el .toml (organizado en secciones [general]/[trade]/[strategy]/
    [pumpportal] solo por legibilidad) y arma UNA única AppConfig con todos
    los campos juntos."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    valid_keys = {f.name for f in fields(AppConfig)}
    merged: dict = {}
    for section_name in ("general", "trade", "strategy", "pumpportal", "jupiter", "onchain", "web"):
        section = raw.get(section_name, {})
        unknown = set(section) - valid_keys
        if unknown:
            logger.warning(f"Claves desconocidas en el .toml para [{section_name}]: {sorted(unknown)}")
        merged.update({k: v for k, v in section.items() if k in valid_keys})

    config = AppConfig(**merged)

    # BUGFIX: el .toml de ejemplo y los mensajes de error de aquí abajo
    # siempre dijeron que la api_key también se podía definir con la
    # variable de entorno PUMPPORTAL_API_KEY (para no tener que escribirla
    # en el archivo), pero nunca se leía realmente -> quien confiara en
    # esa opción se encontraba con "Falta la API key" de todos modos. Si
    # el .toml no trae una key, ahora sí se consulta la variable de entorno como
    # fallback antes de fallar.
    if not config.web_password:
        config.web_password = os.environ.get("PEPUMP_WEB_PASSWORD", "")

    if not config.api_key:
        config.api_key = os.environ.get("PUMPPORTAL_API_KEY", "")
    config.api_key = config.api_key.strip()
    config.wallet_address = config.wallet_address.strip()
    if not config.jupiter_api_key:
        config.jupiter_api_key = os.environ.get("JUPITER_API_KEY", "")
    config.jupiter_api_key = config.jupiter_api_key.strip()
    if config.wallet_private_key:
        _warn_if_readable_by_others(path)
    else:
        config.wallet_private_key = os.environ.get("PEPUMP_WALLET_PRIVATE_KEY", "")
    config.wallet_private_key = config.wallet_private_key.strip()

    if not config.api_key:
        raise ValueError(
            "Falta la API key de PumpPortal (pumpportal.api_key en el .toml, o la variable de "
            "entorno PUMPPORTAL_API_KEY). Es obligatoria SIEMPRE, aunque general.live = false: "
            "el bot usa únicamente subscribeTokenTrade para el precio, y ese feed requiere API "
            "key + wallet con al menos 0.02 SOL para entregar trades."
        )

    if raw.get("general", {}).get("mint"):
        logger.warning("El .toml define general.mint, pero el mint se toma SIEMPRE de la línea de "
                       "comandos (-m/--mint). El valor del archivo se ignora.")

    validate_config(config)
    return config
