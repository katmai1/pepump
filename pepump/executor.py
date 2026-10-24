import asyncio
import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from pepump.history import append_closed_trade
from pepump.jupiter import JUPITER_POOL, PRIORITY_TRADE
from pepump.pumpportal import TradeOutcomeUnknown

logger = logging.getLogger(__name__)


@dataclass
class Position:
    mint: str
    entry_price: float                 # precio en SOL por token, al comprar
    sol_amount: float                  # SOL invertidos (sin rent, ver abajo)
    token_amount: float                # tokens obtenidos
    # Precio de MERCADO de referencia en el momento de comprar (el que
    # venía del feed/on-chain). Se separa de `entry_price` porque, con
    # datos reales de fill, `entry_price` es el precio EFECTIVO pagado
    # e incluye los costes de la tx (comisiones, priority fee...).
    #
    # El RENT de las cuentas de token no cuenta en NADA de la posición
    # (entry_price, sol_amount, proceeds_sol, PnL): es un depósito de la
    # wallet, no un coste. Las compras y ventas lo pagan o lo recuperan
    # al crear y cerrar cuentas -la del token y las de los tokens
    # intermedios de una ruta de Jupiter-, y el bot lo recupera aparte
    # cerrando las cuentas vacías (ver token_accounts.py). Intentar
    # atribuirlo a cada posición no cuadraba nunca: el barrido del
    # arranque cerraba cuentas de posiciones abiertas, la venta las volvía
    # a crear... (caso real: un swap de +3.7% registrado como -4.9%). Así,
    # el PnL es el del swap, el mismo que enseña Jupiter.
    #
    # Ese precio efectivo es el
    # correcto para calcular el PnL real, pero NO para los umbrales de
    # la estrategia: comparar el precio de mercado actual contra un
    # precio inflado por comisiones desplaza en silencio la activación
    # del trailing y el stop-loss inicial. Los umbrales usan esto; el
    # PnL usa entry_price. Si no se pasa, vale lo mismo que entry_price
    # (modo simulado, o compra real sin fill real -> comportamiento
    # idéntico al de antes).
    market_entry_price: float = 0.0
    highest_price: float = field(init=False)
    armed: bool = False                # ¿ya se activó el trailing stop?
    armed_at: Optional[float] = None   # time.time() de cuándo se armó (para el gráfico)
    closed: bool = False
    # True si entry_price/sol_amount/token_amount salen de los balances
    # REALES pre/post de la transacción de compra confirmada (ver
    # TradeExecutor.buy) en vez de ser una estimación a partir del
    # precio de referencia. Solo puede ser True en modo REAL, y solo si
    # se pudo leer la tx (ver onchain/txinfo.py:fetch_actual_fill) -si esa
    # lectura falla, se cae al estimado y esto queda en False aunque la
    # compra haya sido real.
    entry_is_real_fill: bool = False
    # time.time() del momento en que se abrió la posición (Position se crea
    # solo al comprar -ver TradeExecutor.buy-, así que esto es "ahora"
    # salvo que se pise explícitamente, como hacen los tests). Sirve para
    # calcular cuánto duró abierta la posición en el historial de CSV.
    opened_at: float = field(default_factory=time.time)
    # Datos del cierre, los rellena TradeExecutor.sell() al confirmar la
    # venta (None mientras siga abierta). Los mismos números que van al
    # historial CSV, para poder mostrarlos sin releer el archivo.
    closed_at: Optional[float] = None
    exit_price: Optional[float] = None
    proceeds_sol: Optional[float] = None
    realized_pnl_sol: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    close_reason: Optional[str] = None
    exit_is_real_fill: bool = False
    # Tokens intermedios de la ruta de compra (Jupiter: SOL -> USDC -> TTWO
    # -> token) cuyas cuentas creó la compra en la wallet: se cierran tras
    # vender para recuperar su rent en la wallet. No se guarda en la base de
    # datos: si el bot se reinicia entre medias, las recoge el barrido del
    # arranque (y como el rent no cuenta en el PnL, da igual quién las cierre).
    route_mints: list = field(default_factory=list)

    def __post_init__(self):
        if not self.market_entry_price or self.market_entry_price <= 0:
            self.market_entry_price = self.entry_price
        # El máximo se compara siempre contra precios de MERCADO, así que
        # arranca en el precio de mercado de la entrada, no en el efectivo.
        self.highest_price = self.market_entry_price

    def pnl_pct(self, price: float) -> float:
        """PnL NETO: cuánto ganarías/perderías de verdad si vendieras a
        `price`, medido contra el precio EFECTIVO que pagaste. Con datos
        reales de fill eso incluye las comisiones y el priority fee -por
        eso arranca en negativo aunque el precio no se haya movido-, pero
        no el rent (ver market_entry_price)."""
        return (price / self.entry_price - 1.0) * 100.0

    def market_pnl_pct(self, price: float) -> float:
        """PnL de MERCADO: cuánto se movió el precio desde que compraste,
        sin contar ningún coste de la operación. Es el número comparable
        con el que muestra pump.fun (y con el que se miden los umbrales
        de la estrategia)."""
        return (price / self.market_entry_price - 1.0) * 100.0

    def strategy_entry_price(self, basis: str) -> float:
        """Precio de entrada contra el que se miden los umbrales de la
        estrategia (activación del trailing y stop-loss inicial), según
        `strategy.strategy_price_basis`: "real" -> entry_price (lo que se
        pagó de verdad), "market" -> market_entry_price."""
        return self.market_entry_price if basis == "market" else self.entry_price

    def strategy_pnl_pct(self, price: float, basis: str) -> float:
        """El % que corresponde a strategy_entry_price: neto con "real",
        de mercado con "market"."""
        return self.market_pnl_pct(price) if basis == "market" else self.pnl_pct(price)

    def has_entry_costs(self) -> bool:
        """True si entry_price incluye los costes de entrar: datos reales de
        la tx, o costes simulados (general.sim_costs). Ahí el PnL neto y el
        de mercado son números distintos y vale la pena mostrar los dos."""
        return self.entry_is_real_fill or self.entry_price != self.market_entry_price

    def cost_label(self) -> str:
        return "coste real" if self.entry_is_real_fill else "coste simulado"

    def entry_cost_pct(self) -> float:
        """Cuánto por encima del precio de mercado te salió entrar, en %.
        0 si no hay datos reales de fill (ahí ambos precios son el
        mismo). Es exactamente la brecha entre `market_pnl_pct` y
        `pnl_pct`."""
        if not self.market_entry_price:
            return 0.0
        return (self.entry_price / self.market_entry_price - 1.0) * 100.0


# Cuánto sube cada venta fallida: +SELL_SLIPPAGE_STEP puntos de slippage y
# priority fee x SELL_PRIORITY_FEE_FACTOR, hasta los topes de la config.
# Con los valores por defecto: slippage 15 -> 25 -> 35 -> 45 -> 50 y fee
# 0.00001 -> 0.00004 -> 0.00016 -> 0.00064 -> 0.001 SOL.
SELL_SLIPPAGE_STEP = 10.0
# Con cuántos tokens de más (en tanto por uno) vendidos que los de la
# posición se considera que la venta se llevó tokens ajenos a ella (ver
# sell). Por debajo es redondeo.
_EXTRA_TOKENS_TOLERANCE = 0.01
SELL_PRIORITY_FEE_FACTOR = 4.0


# Costes fijos de una operación real que también se cobran al simular
# (ver simulated_buy / simulated_sell). El rent de la cuenta del token no:
# no es un coste (ver Position.market_entry_price).
NETWORK_FEE_SOL = 0.000005  # fee base de una tx de Solana (una firma)


def simulated_buy(cfg, price: float) -> tuple[float, float, float]:
    """Compra SIMULADA con los costes de una real (con cfg.sim_costs):
    se paga `sim_slippage_pct` % por encima del precio de mercado, más
    `sim_fee_pct` % de comisiones (pump.fun + PumpPortal) sobre buy_sol,
    el priority fee y el fee de red. Devuelve (SOL gastados, tokens
    recibidos, precio efectivo), los mismos tres números que da una compra
    real con datos de fill."""
    fill_price = price * (1 + cfg.sim_slippage_pct / 100.0)
    tokens = cfg.buy_sol / fill_price
    spent = cfg.buy_sol * (1 + cfg.sim_fee_pct / 100.0) + cfg.priority_fee + NETWORK_FEE_SOL
    return spent, tokens, spent / tokens


def simulated_sell(cfg, tokens: float, price: float,
                   priority_fee: Optional[float] = None, fee_pct: Optional[float] = None) -> tuple[float, float]:
    """Venta SIMULADA con los costes de una real (con cfg.sim_costs): se
    cobra `sim_slippage_pct` % por debajo del precio de mercado, menos
    `sim_fee_pct` % de comisiones (o `fee_pct`), el priority fee y el fee
    de red. Devuelve (SOL recibidos, precio efectivo)."""
    fee = cfg.priority_fee if priority_fee is None else priority_fee
    fee_pct = cfg.sim_fee_pct if fee_pct is None else fee_pct
    gross = tokens * price * (1 - cfg.sim_slippage_pct / 100.0)
    proceeds = gross * (1 - fee_pct / 100.0) - fee - NETWORK_FEE_SOL
    # Sin ingresos netos no hay precio de salida con sentido (igual que en
    # una venta real): se deja el de mercado.
    exit_price = proceeds / tokens if proceeds > 0 and tokens > 0 else price
    return proceeds, exit_price


def _rent_delta(result) -> float:
    """Rent neto que pagó una tx (pagado al crear cuentas de token de la
    wallet menos devuelto al cerrarlas), según el fill real."""
    if not isinstance(result, dict):
        return 0.0
    return (result.get("actual_rent_paid_sol") or 0.0) - (result.get("actual_rent_refund_sol") or 0.0)


def sell_retry_params(cfg, failures: int) -> tuple[float, float]:
    """(slippage, priority_fee) para una venta tras `failures` intentos
    fallidos seguidos. Con 0 fallos son los de la config, tal cual.

    Los topes (max_sell_slippage / max_sell_priority_fee) nunca bajan los
    valores base: si una posición se abrió con un slippage mayor que el
    tope, se vende con el suyo."""
    slippage = min(cfg.slippage + SELL_SLIPPAGE_STEP * failures,
                   max(cfg.max_sell_slippage, cfg.slippage))
    priority_fee = min(cfg.priority_fee * SELL_PRIORITY_FEE_FACTOR ** failures,
                       max(cfg.max_sell_priority_fee, cfg.priority_fee))
    return slippage, priority_fee


# --------------------------------------------------------------------------- #
# Ejecutor de trades: decide y ejecuta compra/venta, simulada o real
# --------------------------------------------------------------------------- #

class TradeExecutor:
    """
    Gestiona la compra y venta de un token, ya sea en modo SIMULADO (no toca
    la red, solo lleva la cuenta en memoria) o en modo REAL (usa un
    PumpPortalClient para mandar la orden a la Lightning API, o el
    JupiterClient si la posición va por Jupiter: pool_override="jupiter").
    """

    def __init__(self, client, live: bool, config, jupiter=None, closer=None):
        self.client = client
        self.live = live
        self.cfg = config
        self.jupiter = jupiter
        # TokenAccountCloser (ver token_accounts.py), o None: cierra la
        # cuenta del token que queda vacía tras una venta real.
        self.closer = closer

    def _jupiter(self):
        if self.jupiter is None:
            raise RuntimeError("Esta posición va por Jupiter y no hay jupiter_api_key configurada.")
        return self.jupiter

    async def _execute(self, pool: str, **kwargs) -> dict:
        if pool == JUPITER_POOL:
            return await self._jupiter().execute_trade(**kwargs)
        return await self.client.execute_lightning_trade(pool=pool, **kwargs)

    async def _simulated_jupiter_buy(self, mint: str, price: float) -> tuple[float, float, float]:
        """Como simulated_buy, pero los tokens salen de una cotización de
        compra real de Jupiter (ruta, comisiones e impacto ya incluidos), en
        vez de aplicar sim_fee_pct a un precio. Si la cotización falla, se
        cae a simulated_buy con el precio de referencia."""
        try:
            tokens = await self._jupiter().buy_tokens(mint, self.cfg.buy_sol, self.cfg.solana_rpc_url,
                                                      priority=PRIORITY_TRADE)
        except Exception as e:
            logger.warning(f"[SIMULADO] No se pudo cotizar la compra en Jupiter ({e}); "
                           f"se simula con el precio de referencia.")
            return simulated_buy(self.cfg, price)
        tokens *= 1 - self.cfg.sim_slippage_pct / 100.0
        spent = self.cfg.buy_sol + self.cfg.priority_fee + NETWORK_FEE_SOL
        return spent, tokens, spent / tokens

    # Intentos de cerrar la cuenta del token justo después de vender, y pausa
    # entre ellos: el nodo RPC puede tardar un poco en ver la venta.
    CLOSE_ACCOUNT_ATTEMPTS = 3
    CLOSE_ACCOUNT_RETRY_SECONDS = 2.0

    async def _close_token_accounts(self, mint: str, route_mints=()) -> float:
        """Tras una venta real: cierra la cuenta del token y las de los
        tokens intermedios de la ruta que hayan quedado vacías, para
        recuperar su rent en la wallet. Reintenta mientras el nodo aún no
        vea la venta o algún cierre falle. Devuelve los SOL recuperados, solo
        para el log: el rent no cuenta en el PnL (ver
        Position.market_entry_price). Lo que no se pueda cerrar lo recoge el
        barrido del próximo arranque."""
        if self.closer is None:
            return 0.0
        total, motivo = 0.0, ""
        for intento in range(1, self.CLOSE_ACCOUNT_ATTEMPTS + 1):
            try:
                recuperado, cerradas, fallidas, pendiente = await self.closer.close_after_sale(mint, route_mints)
            except Exception as e:
                fallidas, pendiente, motivo = 1, False, str(e)
            else:
                total += recuperado
                if cerradas:
                    logger.info(f"[REAL] {cerradas} cuenta(s) de token cerradas: recuperados "
                                f"{recuperado:.9f} SOL de rent en la wallet.")
                if not fallidas and not pendiente:
                    return total
                motivo = ("la cuenta del token todavía no aparece vacía" if pendiente else
                          "el cierre falló (ejecuta con -v para ver el motivo)")
            if intento < self.CLOSE_ACCOUNT_ATTEMPTS:
                await asyncio.sleep(self.CLOSE_ACCOUNT_RETRY_SECONDS)
        logger.warning(f"[REAL] No se pudieron cerrar todas las cuentas vacías de la operación de {mint} "
                       f"({motivo}); su rent se recupera al volver a arrancar el bot.")
        return total

    async def _simulated_jupiter_sell(self, mint: str, tokens: float, price: float,
                                      priority_fee: Optional[float]) -> tuple[float, float]:
        """Como simulated_sell, pero con lo que Jupiter daría de verdad por
        los tokens (comisiones de la ruta incluidas) en vez de cobrar
        sim_fee_pct sobre el precio: el precio que vigila el bot puede ser
        el del pool on-chain, sin comisiones (Meteora). Si la cotización
        falla, se cae a simulated_sell con el precio de mercado."""
        try:
            quoted = await self._jupiter().sell_price(mint, tokens, self.cfg.solana_rpc_url,
                                                      priority=PRIORITY_TRADE)
        except Exception as e:
            logger.warning(f"[SIMULADO] No se pudo cotizar la venta en Jupiter ({e}); "
                           f"se simula con el precio de mercado.")
            return simulated_sell(self.cfg, tokens, price, priority_fee)
        return simulated_sell(self.cfg, tokens, quoted, priority_fee, fee_pct=0.0)

    async def buy(self, mint: str, price: float, pool_override: Optional[str] = None) -> Position:
        # Valores ESTIMADOS a partir del precio de referencia -se usan
        # tal cual en modo SIMULADO, y como respaldo en modo REAL si no
        # se pudieron leer los datos reales de fill (ver más abajo).
        sol_amount = self.cfg.buy_sol
        entry_price = price
        token_amount = sol_amount / price if price > 0 else 0.0
        real_fill = False
        route_mints: list = []

        if not self.live and self.cfg.sim_costs:
            if pool_override == JUPITER_POOL:
                sol_amount, token_amount, entry_price = await self._simulated_jupiter_buy(mint, price)
            else:
                sol_amount, token_amount, entry_price = simulated_buy(self.cfg, price)

        if self.live:
            try:
                result = await self._execute(
                    pool_override or self.cfg.pool,
                    action="buy", mint=mint, amount=sol_amount, denominated_in_sol=True,
                    slippage=self.cfg.slippage, priority_fee=self.cfg.priority_fee,
                    solana_rpc_url=self.cfg.solana_rpc_url,
                    tx_confirm_timeout_seconds=self.cfg.tx_confirm_timeout_seconds,
                    tx_confirm_poll_interval_seconds=self.cfg.tx_confirm_poll_interval_seconds,
                )
                logger.info(f"[REAL] Compra CONFIRMADA on-chain. Respuesta: {result}")
            except TradeOutcomeUnknown as e:
                # La orden pudo haber entrado (timeout de la API, tx sin
                # confirmar a tiempo). Antes se daba por fallida sin más: la
                # posición quedaba "cancelada" y, si la tx entraba, los tokens
                # se quedaban en la wallet sin trailing ni stop. Se mira la
                # wallet, como en sell().
                balance = await self._poll_wallet_balance(mint, lambda b: b > 0)
                if balance is None:
                    logger.error(f"[REAL] La compra de {mint} no se confirmó ({e}) y no se pudo comprobar "
                                 f"la wallet: revisa a mano si se compró.")
                    raise
                if balance <= 0:
                    logger.error(f"[REAL] La compra de {mint} no se confirmó ({e}) y no llegaron tokens a "
                                 f"la wallet: no se abre ninguna posición.")
                    raise RuntimeError(f"la compra no llegó a la wallet ({e})") from e
                logger.warning(f"[REAL] La compra de {mint} no se confirmó ({e}), pero la wallet ya tiene "
                               f"{balance:,.2f} tokens: la compra SÍ se ejecutó. Se abre la posición con "
                               f"la entrada ESTIMADA (no hay firma de la que leer los datos reales).")
                token_amount = balance
                result = {}
            except Exception:
                # execute_lightning_trade ahora confirma en cadena antes
                # de devolver: si esto lanza, es porque la orden fue
                # rechazada de inmediato, o la tx confirmó pero FALLÓ on-chain
                # (p. ej. slippage excedido), o no confirmó a tiempo. En
                # NINGUNO de esos casos hay que abrir una posición -no se
                # compró nada de verdad-, así que propagamos el error y
                # no seguimos adelante.
                logger.exception("[REAL] Error al comprar (la orden no se confirmó on-chain)")
                raise

            # La compra CONFIRMÓ on-chain. execute_lightning_trade ahora
            # devuelve, además de la respuesta cruda de la Lightning
            # API, los datos REALES de fill (leídos de los balances
            # pre/post de la propia transacción) en "actual_sol_delta" /
            # "actual_token_delta" -ver onchain/txinfo.py:fetch_actual_fill. Si
            # están, reemplazan la estimación con los números reales de
            # lo que efectivamente pasó en la wallet (incluye fees).
            actual_sol_delta = result.get("actual_sol_delta") if isinstance(result, dict) else None
            actual_token_delta = result.get("actual_token_delta") if isinstance(result, dict) else None
            # Una compra real SIEMPRE deja sol_delta negativo (se gasta
            # SOL) y token_delta positivo (entran tokens). Si no se
            # cumple, algo no cuadra con lo que leímos -mejor el
            # estimado que un precio de entrada inventado.
            if (actual_sol_delta is not None and actual_token_delta is not None
                    and actual_token_delta > 0 and actual_sol_delta < 0):
                # El rent de las cuentas que crea (o cierra) la compra no es
                # coste: se descuenta (ver Position.market_entry_price).
                rent = _rent_delta(result)
                real_sol_spent = abs(actual_sol_delta) - rent
                if real_sol_spent <= 0:
                    real_sol_spent, rent = abs(actual_sol_delta), 0.0
                real_token_amount = actual_token_delta
                route_mints = [m for m in (result.get("actual_created_mints") or []) if m != mint]
                sol_amount = real_sol_spent
                token_amount = real_token_amount
                entry_price = real_sol_spent / real_token_amount
                real_fill = True
                sobrecoste_pct = ((entry_price / price - 1.0) * 100.0) if price > 0 else 0.0
                sobrecoste_sol = real_sol_spent - self.cfg.buy_sol
                logger.info(f"[REAL] Datos REALES de la compra (de la tx confirmada, incluyen fees): "
                            f"gastaste {real_sol_spent:.9f} SOL y recibiste {real_token_amount:,.6f} "
                            f"tokens -> precio efectivo real: {entry_price:.10f} SOL/token "
                            f"({sobrecoste_pct:+.2f}% vs. el precio de mercado de referencia "
                            f"{price:.10f}).")
                renta = (f". Aparte, {rent:.9f} SOL de rent de las cuentas de token creadas: es un "
                         f"depósito que se recupera al cerrarlas, no un coste" if rent else "")
                logger.info(f"[REAL] Coste de entrar: {sobrecoste_sol:+.9f} SOL sobre los "
                            f"{self.cfg.buy_sol:.9f} SOL de la orden (comisiones del pool y del "
                            f"intermediario, priority fee y fee de red){renta}. Por eso el % que "
                            f"muestra pump.fun -que es solo movimiento de precio- va a ir "
                            f"{sobrecoste_pct:+.2f}% por encima del PnL neto de pepump.")
            else:
                logger.warning(f"[REAL] No se pudieron confirmar los datos reales de la compra; "
                                f"se usa el ESTIMADO (precio de referencia {price:.10f} SOL/token, "
                                f"~{token_amount:,.2f} tokens) para la posición.")

        etiqueta = "REAL" if self.live else "SIMULADO"
        sufijo = self._fill_suffix(real_fill)
        logger.info(f"[{etiqueta}] COMPRA de {sol_amount:.9f} SOL en {mint} a precio "
                    f"{entry_price:.10f} SOL/token (~{token_amount:,.2f} tokens){sufijo}")
        return Position(mint=mint, entry_price=entry_price, sol_amount=sol_amount,
                         token_amount=token_amount, market_entry_price=price,
                         entry_is_real_fill=real_fill, route_mints=route_mints)

    def _fill_suffix(self, real_fill: bool) -> str:
        if self.live:
            return " [datos reales]" if real_fill else " [estimado]"
        return " [con costes simulados]" if self.cfg.sim_costs else ""

    async def _poll_wallet_balance(self, mint: str, until) -> Optional[float]:
        """Saldo de `mint` en la wallet en cuanto cumple `until(saldo)`, o el
        último leído al pasar tx_confirm_timeout_seconds: una tx enviada
        justo antes del timeout puede tardar unos segundos más en entrar.
        None si no se puede consultar (wallet todavía desconocida, RPC
        caído...)."""
        deadline = time.monotonic() + self.cfg.tx_confirm_timeout_seconds
        while True:
            balance = await self.client.get_wallet_token_balance(mint, self.cfg.solana_rpc_url)
            if balance is None or until(balance) or time.monotonic() >= deadline:
                return balance
            await asyncio.sleep(self.cfg.tx_confirm_poll_interval_seconds)

    async def _tokens_gone_from_wallet(self, mint: str) -> bool:
        """True si la wallet ya no tiene tokens de `mint`. False si quedan
        tokens al final o si no se puede consultar el saldo: ante la duda,
        la posición se sigue considerando abierta."""
        balance = await self._poll_wallet_balance(mint, lambda b: b <= 0)
        return balance is not None and balance <= 0

    @staticmethod
    def _log_sell_failure(position: Position) -> None:
        # BUGFIX: antes esta excepción se logueaba y se seguía de largo
        # igual: la posición se marcaba `closed = True` y se imprimía el
        # PnL/proceeds como si la venta hubiese salido bien, cuando en
        # realidad la Lightning API falló (o la tx confirmó pero FALLÓ
        # on-chain, p. ej. por slippage excedido) y el token real seguía en
        # la wallet, sin vender. Con SOL real eso es peligroso: el bot "cree"
        # que cerró la posición y deja de vigilarla. Ahora se propaga la
        # excepción y NO se marca la posición como cerrada, para que quede
        # claro que la venta real falló y haya que reintentarla (bot.py
        # reintenta solo con el próximo precio que llegue — ver _try_sell).
        # Se llama dentro del `except`: logger.exception lleva el traceback.
        logger.exception(
            f"[REAL] Error al vender {position.mint} (la orden no se confirmó on-chain). "
            "La posición SIGUE ABIERTA: no se marcó como vendida."
        )

    async def sell(self, position: Position, price: float, reason: str,
                    pool_override: Optional[str] = None, slippage: Optional[float] = None,
                    priority_fee: Optional[float] = None) -> None:
        """`slippage`/`priority_fee` pisan los de la config (reintentos de
        una venta fallida, ver sell_retry_params); None = los de la config."""
        # Valores ESTIMADOS a partir del precio de referencia -se usan
        # tal cual en modo SIMULADO, y como respaldo en modo REAL si no
        # se pudieron leer los datos reales de fill (ver más abajo).
        exit_price = price
        token_amount_sold = position.token_amount
        proceeds = token_amount_sold * price
        real_fill = False

        if not self.live and self.cfg.sim_costs:
            if pool_override == JUPITER_POOL:
                proceeds, exit_price = await self._simulated_jupiter_sell(
                    position.mint, token_amount_sold, price, priority_fee)
            else:
                proceeds, exit_price = simulated_sell(self.cfg, token_amount_sold, price, priority_fee)

        if self.live:
            try:
                result = await self._execute(
                    pool_override or self.cfg.pool,
                    action="sell", mint=position.mint, amount="100%", denominated_in_sol=False,
                    slippage=self.cfg.slippage if slippage is None else slippage,
                    priority_fee=self.cfg.priority_fee if priority_fee is None else priority_fee,
                    solana_rpc_url=self.cfg.solana_rpc_url,
                    tx_confirm_timeout_seconds=self.cfg.tx_confirm_timeout_seconds,
                    tx_confirm_poll_interval_seconds=self.cfg.tx_confirm_poll_interval_seconds,
                )
                logger.info(f"[REAL] Venta CONFIRMADA on-chain. Respuesta: {result}")
            except TradeOutcomeUnknown as e:
                # La orden pudo haber salido igual (timeout de la Lightning
                # API o tx sin confirmar a tiempo). Antes esto se trataba
                # como venta fallida aunque la tx hubiese entrado: el bot
                # dejaba la posición "abierta" sin tokens reales detrás (y
                # al reintentar, el "100%" fallaba por no quedar nada).
                # Se mira la wallet: si ya no tiene tokens, la venta salió.
                if not await self._tokens_gone_from_wallet(position.mint):
                    logger.error(
                        f"[REAL] Error al vender {position.mint}: {e}. Los tokens siguen en la "
                        "wallet (o no se pudo comprobar): la posición SIGUE ABIERTA."
                    )
                    raise
                logger.warning(
                    f"[REAL] La Lightning API no confirmó la venta de {position.mint} ({e}), pero "
                    "la wallet ya no tiene tokens de ese mint: la venta SÍ se ejecutó. Se registra "
                    "con el PnL ESTIMADO (no hay firma de la que leer los datos reales)."
                )
                result = {}
            except Exception as e:
                # Un rechazo puede ser justo porque ya NO quedan tokens: la
                # venta anterior entró tarde (tras dar su resultado por
                # desconocido) o se vendió a mano fuera del bot. Sin mirar la
                # wallet, PumpPortal rechazaba cada "100%" y el bot
                # reintentaba sin fin con la posición "abierta". Una lectura
                # basta: si quedan tokens, el fallo es de verdad.
                balance = await self.client.get_wallet_token_balance(position.mint, self.cfg.solana_rpc_url)
                if balance is None or balance > 0:
                    self._log_sell_failure(position)
                    raise
                logger.warning(
                    f"[REAL] La venta de {position.mint} falló ({e}), pero la wallet ya no tiene "
                    "tokens de ese mint: ya se vendieron (fuera del bot o en una venta anterior que "
                    "entró tarde). Se cierra la posición con el PnL ESTIMADO."
                )
                result = {}

            # La venta CONFIRMÓ on-chain (o ya no quedan tokens que vender).
            # Igual que en buy(), reemplazamos
            # la estimación con los datos REALES de fill si se pudieron
            # leer -ver onchain/txinfo.py:fetch_actual_fill.
            actual_sol_delta = result.get("actual_sol_delta") if isinstance(result, dict) else None
            actual_token_delta = result.get("actual_token_delta") if isinstance(result, dict) else None
            # La señal de que la venta se leyó bien es el delta de TOKENS
            # (negativo: salieron de la wallet). El delta de SOL no sirve
            # como condición: en una posición pequeña que se hunde, las
            # comisiones + priority fee pueden superar a lo que se recibe
            # y dejarlo en cero o negativo -y ahí caer al ESTIMADO es lo
            # peor que se puede hacer, porque el CSV registraría unos
            # ingresos que nunca existieron.
            if actual_sol_delta is not None and actual_token_delta is not None and actual_token_delta < 0:
                # recibimos SOL -> sol_delta viene positivo. Sin el rent de
                # las cuentas que la venta crea (la ruta puede volver a crear
                # las intermedias) o cierra (ver Position.market_entry_price).
                real_sol_received = actual_sol_delta + _rent_delta(result)
                real_token_amount_sold = abs(actual_token_delta)  # vendimos tokens -> viene negativo
                proceeds = real_sol_received
                token_amount_sold = real_token_amount_sold
                if real_sol_received > 0 and real_token_amount_sold > 0:
                    exit_price = real_sol_received / real_token_amount_sold
                else:
                    # Sin ingresos netos no hay "precio de salida" que
                    # tenga sentido: se deja el de mercado para el log/CSV
                    # y el PnL sale de todos modos de los SOL reales.
                    logger.warning(f"[REAL] La venta de {position.mint} no dejó SOL neto positivo "
                                    f"({real_sol_received:.9f} SOL): las comisiones se comieron los "
                                    f"ingresos. Se registra el PnL real de todos modos.")
                    exit_price = price
                real_fill = True
                logger.info(f"[REAL] Datos REALES de la venta (de la tx confirmada, ya netos de fees): "
                            f"vendiste {real_token_amount_sold:,.6f} tokens y recibiste "
                            f"{real_sol_received:.9f} SOL -> precio efectivo real: {exit_price:.10f} "
                            f"SOL/token.")
                # La venta es del 100% de la wallet: si había tokens de más
                # (un resto anterior, una compra a mano), sus SOL contaban
                # como ganancia de la posición. Solo cuenta la parte de ella.
                # Sin ingresos positivos no hay nada que repartir: las
                # comisiones son de la tx, no de cada token.
                if (position.token_amount > 0 and real_sol_received > 0
                        and real_token_amount_sold > position.token_amount * (1 + _EXTRA_TOKENS_TOLERANCE)):
                    parte = position.token_amount / real_token_amount_sold
                    proceeds = real_sol_received * parte
                    token_amount_sold = position.token_amount
                    logger.warning(f"[REAL] Se vendieron {real_token_amount_sold:,.6f} tokens de {position.mint} "
                                   f"y la posición tenía {position.token_amount:,.6f}: el resto ya estaba en la "
                                   f"wallet. El PnL cuenta solo la parte de la posición "
                                   f"({proceeds:.9f} de {real_sol_received:.9f} SOL).")
            else:
                logger.warning("[REAL] No se pudieron confirmar los datos reales de la venta; "
                               "se usa el ESTIMADO (precio de referencia y token_amount de la "
                               "posición) para el PnL.")

            # Ni PumpPortal ni Jupiter cierran la cuenta del token al vender
            # todo, ni las de los tokens intermedios de la ruta: su rent se
            # queda dentro. Se cierran aquí para recuperarlo en la wallet; no
            # cuenta en el PnL (ver Position.market_entry_price).
            creadas = result.get("actual_created_mints") if isinstance(result, dict) else None
            await self._close_token_accounts(position.mint, set(position.route_mints) | set(creadas or []))

        # PnL calculado en SOL real gastado vs. SOL real recibido -no
        # como ratio de precios-, para que en modo REAL con datos reales
        # (real_fill=True) refleje EXACTAMENTE lo que pasó en la wallet
        # (compra y venta ya vienen netas de fees en ese caso). En modo
        # SIMULADO o si no hay datos reales, position.sol_amount y
        # proceeds son ambos estimados a partir de precios, y da el
        # mismo resultado que el cálculo anterior basado en precios.
        pnl_sol = proceeds - position.sol_amount
        pnl_pct = (pnl_sol / position.sol_amount * 100.0) if position.sol_amount > 0 else 0.0
        etiqueta = "REAL" if self.live else "SIMULADO"
        sufijo = self._fill_suffix(real_fill)
        # Se loguean los DOS números porque miden cosas distintas y no
        # coinciden en cuanto hay costes reales de por medio: el de
        # mercado es cuánto se movió el precio (el que muestra pump.fun),
        # el neto es lo que de verdad entró y salió de la wallet.
        # BUGFIX: se medía contra `exit_price`, que con datos reales de
        # fill es el precio EFECTIVO (ya neto de comisiones de venta) —
        # o sea, el número "de mercado" venía descontando fees y no era
        # comparable con el que muestra pump.fun, que es justo para lo
        # que existe. El movimiento de precio se mide siempre contra el
        # precio de MERCADO con el que se disparó la venta.
        pnl_mercado = position.market_pnl_pct(price)
        logger.info(f"[{etiqueta}] VENTA de {position.mint} a precio {exit_price:.10f} SOL/token "
                    f"| motivo: {reason} | mercado: {pnl_mercado:+.2f}% | PnL neto: {pnl_pct:+.2f}% "
                    f"({pnl_sol:+.9f} SOL) | SOL recibidos: {proceeds:.9f}{sufijo}")
        position.closed_at = time.time()
        position.exit_price = exit_price
        position.proceeds_sol = proceeds
        position.realized_pnl_sol = pnl_sol
        position.realized_pnl_pct = pnl_pct
        position.close_reason = reason
        position.exit_is_real_fill = real_fill
        position.closed = True

        # Historial de órdenes cerradas: se registra DESPUÉS de marcar
        # closed = True (la venta ya está confirmada -real u obligada a
        # "éxito" en modo simulado-, así que solo faltaría persistir el
        # registro). Un problema al escribir el CSV nunca debe hacer
        # parecer que la venta falló -ver append_closed_trade, que absorbe
        # sus propios errores de disco.
        history_path = getattr(self.cfg, "trade_history_csv", "") or ""
        append_closed_trade(history_path, {
            "closed_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "mint": position.mint,
            "mode": etiqueta,
            "entry_price": f"{position.entry_price:.10f}",
            "exit_price": f"{exit_price:.10f}",
            "sol_amount": f"{position.sol_amount:.9f}",
            "token_amount": f"{token_amount_sold:.6f}",
            "proceeds_sol": f"{proceeds:.9f}",
            "pnl_pct": f"{pnl_pct:.4f}",
            "duration_seconds": f"{max(time.time() - position.opened_at, 0.0):.1f}",
            "reason": reason,
        })


