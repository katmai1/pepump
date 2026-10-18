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
    sol_amount: float                  # SOL invertidos
    token_amount: float                # tokens obtenidos
    # Precio de MERCADO de referencia en el momento de comprar (el que
    # venía del feed/on-chain). Se separa de `entry_price` porque, con
    # datos reales de fill, `entry_price` es el precio EFECTIVO pagado
    # e incluye los costes de la tx (fee de pump.fun, priority fee...).
    # NO incluye el rent de crear la cuenta del token (~0.002 SOL, un 4%
    # de una compra de 0.05 SOL): se recupera al cerrar la cuenta tras
    # venderlo todo (ver token_accounts.py), así que no es
    # un coste. Sí está en `sol_amount` (lo que salió de la wallet), y el
    # PnL realizado (proceeds - sol_amount) lo cuenta según haya vuelto o
    # no. Ese precio efectivo es el
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

    def __post_init__(self):
        if not self.market_entry_price or self.market_entry_price <= 0:
            self.market_entry_price = self.entry_price
        # El máximo se compara siempre contra precios de MERCADO, así que
        # arranca en el precio de mercado de la entrada, no en el efectivo.
        self.highest_price = self.market_entry_price

    def pnl_pct(self, price: float) -> float:
        """PnL NETO: cuánto ganarías/perderías de verdad si vendieras a
        `price`, medido contra el precio EFECTIVO que pagaste. Con datos
        reales de fill eso incluye la comisión de pump.fun, la de
        PumpPortal, el priority fee y el rent de crear la cuenta de
        token -por eso arranca en negativo aunque el precio no se haya
        movido."""
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
SELL_PRIORITY_FEE_FACTOR = 4.0


# Costes fijos de una operación real que también se cobran al simular
# (ver simulated_buy / simulated_sell).
NETWORK_FEE_SOL = 0.000005  # fee base de una tx de Solana (una firma)
# Rent de la cuenta del token: se paga al comprar y vuelve al vender. Ni
# PumpPortal ni Jupiter cierran la cuenta: la cierra el bot tras la venta
# (ver token_accounts.py; en real, con wallet_private_key).
TOKEN_ACCOUNT_RENT_SOL = 0.00203928


def entry_price_without_rent(spent: float, tokens: float, rent: float) -> float:
    """Precio efectivo de entrada: SOL gastados MENOS el rent de la cuenta
    del token (vuelve al vender), por token."""
    return (spent - rent) / tokens


def simulated_buy(cfg, price: float) -> tuple[float, float, float]:
    """Compra SIMULADA con los costes de una real (con cfg.sim_costs):
    se paga `sim_slippage_pct` % por encima del precio de mercado, más
    `sim_fee_pct` % de comisiones (pump.fun + PumpPortal) sobre buy_sol,
    el priority fee, el fee de red y el rent de la cuenta del token.
    Devuelve (SOL gastados, tokens recibidos, precio efectivo), los mismos
    tres números que da una compra real con datos de fill. El precio
    efectivo no incluye el rent (ver Position.market_entry_price)."""
    fill_price = price * (1 + cfg.sim_slippage_pct / 100.0)
    tokens = cfg.buy_sol / fill_price
    spent = (cfg.buy_sol * (1 + cfg.sim_fee_pct / 100.0) + cfg.priority_fee
             + NETWORK_FEE_SOL + TOKEN_ACCOUNT_RENT_SOL)
    return spent, tokens, entry_price_without_rent(spent, tokens, TOKEN_ACCOUNT_RENT_SOL)


def simulated_sell(cfg, tokens: float, price: float,
                   priority_fee: Optional[float] = None, fee_pct: Optional[float] = None) -> tuple[float, float]:
    """Venta SIMULADA con los costes de una real (con cfg.sim_costs): se
    cobra `sim_slippage_pct` % por debajo del precio de mercado, menos
    `sim_fee_pct` % de comisiones (o `fee_pct`), el priority fee y el fee
    de red, y vuelve el rent de la cuenta del token. Devuelve (SOL
    recibidos, precio efectivo); el precio efectivo no cuenta el rent
    devuelto, igual que el de entrada no cuenta el pagado."""
    fee = cfg.priority_fee if priority_fee is None else priority_fee
    fee_pct = cfg.sim_fee_pct if fee_pct is None else fee_pct
    gross = tokens * price * (1 - cfg.sim_slippage_pct / 100.0)
    proceeds = gross * (1 - fee_pct / 100.0) - fee - NETWORK_FEE_SOL + TOKEN_ACCOUNT_RENT_SOL
    # Sin ingresos netos no hay precio de salida con sentido (igual que en
    # una venta real): se deja el de mercado.
    sin_rent = proceeds - TOKEN_ACCOUNT_RENT_SOL
    exit_price = sin_rent / tokens if sin_rent > 0 and tokens > 0 else price
    return proceeds, exit_price


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
        spent = self.cfg.buy_sol + self.cfg.priority_fee + NETWORK_FEE_SOL + TOKEN_ACCOUNT_RENT_SOL
        return spent, tokens, entry_price_without_rent(spent, tokens, TOKEN_ACCOUNT_RENT_SOL)

    # Intentos de cerrar la cuenta del token justo después de vender, y pausa
    # entre ellos: el nodo RPC puede tardar un poco en ver la venta.
    CLOSE_ACCOUNT_ATTEMPTS = 3
    CLOSE_ACCOUNT_RETRY_SECONDS = 2.0

    async def _recover_token_account_rent(self, mint: str) -> float:
        """Tras una venta real: cierra la cuenta del token si quedó vacía y
        devuelve los SOL recuperados (netos del fee). 0 si no hay closer o si
        no se pudo (lo recoge el barrido del próximo arranque)."""
        if self.closer is None:
            return 0.0
        motivo = "la cuenta todavía no aparece vacía"
        for intento in range(1, self.CLOSE_ACCOUNT_ATTEMPTS + 1):
            try:
                recuperado, cerradas, fallidas = await self.closer.close_empty(mint)
            except Exception as e:
                cerradas, fallidas, motivo = 0, 1, str(e)
            else:
                if cerradas:
                    logger.info(f"[REAL] Cuenta del token cerrada: recuperados {recuperado:.9f} SOL de rent.")
                    return recuperado
                if fallidas:
                    motivo = "el cierre falló (ejecuta con -v para ver el motivo)"
            if intento < self.CLOSE_ACCOUNT_ATTEMPTS:
                await asyncio.sleep(self.CLOSE_ACCOUNT_RETRY_SECONDS)
        logger.warning(f"[REAL] No se pudo cerrar la cuenta vacía de {mint} ({motivo}); su rent se "
                       f"recupera al volver a arrancar el bot.")
        return 0.0

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
                real_sol_spent = abs(actual_sol_delta)  # gastamos SOL -> sol_delta viene negativo
                real_token_amount = actual_token_delta
                rent = result.get("actual_rent_paid_sol") or 0.0
                if not 0 <= rent < real_sol_spent:
                    rent = 0.0
                sol_amount = real_sol_spent
                token_amount = real_token_amount
                entry_price = entry_price_without_rent(real_sol_spent, real_token_amount, rent)
                real_fill = True
                sobrecoste_pct = ((entry_price / price - 1.0) * 100.0) if price > 0 else 0.0
                sobrecoste_sol = real_sol_spent - self.cfg.buy_sol
                logger.info(f"[REAL] Datos REALES de la compra (de la tx confirmada, incluyen fees): "
                            f"gastaste {real_sol_spent:.9f} SOL y recibiste {real_token_amount:,.6f} "
                            f"tokens -> precio efectivo real: {entry_price:.10f} SOL/token "
                            f"({sobrecoste_pct:+.2f}% vs. el precio de mercado de referencia "
                            f"{price:.10f}).")
                renta = (f" y {rent:.9f} SOL de rent de la cuenta del token (se recupera al cerrarla tras vender; "
                         f"no cuenta en el precio de entrada)" if rent else "")
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
                         entry_is_real_fill=real_fill)

    def _fill_suffix(self, real_fill: bool) -> str:
        if self.live:
            return " [datos reales]" if real_fill else " [estimado]"
        return " [con costes simulados]" if self.cfg.sim_costs else ""

    async def _tokens_gone_from_wallet(self, mint: str) -> bool:
        """True si la wallet ya no tiene tokens de `mint`. Va mirando
        durante tx_confirm_timeout_seconds porque una tx enviada justo
        antes del timeout puede tardar unos segundos más en entrar. False
        si quedan tokens al final o si no se puede consultar el saldo
        (wallet todavía desconocida, RPC caído...): ante la duda, la
        posición se sigue considerando abierta."""
        deadline = time.monotonic() + self.cfg.tx_confirm_timeout_seconds
        while True:
            balance = await self.client.get_wallet_token_balance(mint, self.cfg.solana_rpc_url)
            if balance is None:
                return False
            if balance <= 0:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.cfg.tx_confirm_poll_interval_seconds)

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
            except Exception:
                # BUGFIX: antes esta excepción se logueaba y se seguía de
                # largo igual: la posición se marcaba `closed = True` y se
                # imprimía el PnL/proceeds como si la venta hubiese salido
                # bien, cuando en realidad la Lightning API falló (o la tx
                # confirmó pero FALLÓ on-chain, p. ej. por slippage
                # excedido) y el token real seguía en la wallet, sin
                # vender. Con SOL real eso es peligroso: el bot "cree" que
                # cerró la posición y deja de vigilarla. Ahora propagamos
                # la excepción y NO marcamos la posición como cerrada,
                # para que quede claro que la venta real falló y haya que
                # reintentarla (bot.py reintenta solo con el próximo
                # precio que llegue — ver _try_sell).
                logger.exception(
                    f"[REAL] Error al vender {position.mint} (la orden no se confirmó on-chain). "
                    "La posición SIGUE ABIERTA: no se marcó como vendida."
                )
                raise

            # La venta CONFIRMÓ on-chain. Igual que en buy(), reemplazamos
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
                real_sol_received = actual_sol_delta  # recibimos SOL -> sol_delta viene positivo
                real_token_amount_sold = abs(actual_token_delta)  # vendimos tokens -> viene negativo
                proceeds = real_sol_received
                token_amount_sold = real_token_amount_sold
                refund = result.get("actual_rent_refund_sol") or 0.0
                if not 0 <= refund < real_sol_received:
                    refund = 0.0
                if real_sol_received - refund > 0 and real_token_amount_sold > 0:
                    exit_price = (real_sol_received - refund) / real_token_amount_sold
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
            else:
                logger.warning("[REAL] No se pudieron confirmar los datos reales de la venta; "
                               "se usa el ESTIMADO (precio de referencia y token_amount de la "
                               "posición) para el PnL.")

            # Ni PumpPortal ni Jupiter cierran la cuenta del token al vender
            # todo: su rent se queda dentro. Se cierra aquí y se suma a lo
            # recibido (el precio de salida no cambia: no cuenta el rent).
            if not (isinstance(result, dict) and result.get("actual_rent_refund_sol")):
                proceeds += await self._recover_token_account_rent(position.mint)

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


