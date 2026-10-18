"""
Gestor de posiciones: varias posiciones (un TrailingTakeProfitBot por
mint) corriendo en el MISMO proceso, sobre un único feed de PumpPortal.

Es la pieza que controla todo: hoy la usa run.py desde la terminal y más
adelante la usará la web. Cada posición es un bot normal, igual que
cuando se corría uno solo; el gestor solo:

  - los arranca (open), con la config del .toml más los ajustes propios
    que se le pasen a esa posición,
  - impide abrir dos posiciones del mismo mint a la vez (comparten wallet:
    la venta pide el 100% de los tokens del mint, así que la primera en
    vender se llevaría los de la otra),
  - los cierra de forma ordenada (close / close_all -> bot.close(), que
    vende) o los detiene sin vender (stop_all -> bot.stop()),
  - los guarda en SQLite (store.py) y, al arrancar, retoma los que
    quedaron abiertos (recover),
  - y da una foto de todas (snapshot) para mostrarlas.
"""
import asyncio
import dataclasses
import functools
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

from pepump.bot import TrailingTakeProfitBot
from pepump.config import AppConfig, validate_config, validate_mint
from pepump.executor import Position, TradeExecutor
from pepump.feed import SharedTradeFeed
from pepump.jupiter import PRIORITY_BACKGROUND
from pepump.log_context import enable_tags
from pepump.onchain.pump_curve import PumpCurveOnChainClient
from pepump.onchain.pumpswap import PumpSwapOnChainClient
from pepump.store import PositionStore

logger = logging.getLogger(__name__)

# Lo que se puede ajustar por posición al abrirla, incluido si es REAL o
# simulada (`live`; por defecto, general.live del .toml). El resto (RPC,
# api_key, timeouts, límites) es del proceso y sale siempre del .toml.
OVERRIDABLE_FIELDS = (
    "live", "buy_sol", "slippage", "priority_fee", "pool",
    "entry_dip_pct", "activation_pct", "trailing_pct", "initial_stop_pct",
    "strategy_price_basis",
)


@dataclass
class ManagedPosition:
    id: int
    mint: str
    config: AppConfig
    bot: TrailingTakeProfitBot
    task: asyncio.Task

    @property
    def closable(self) -> bool:
        """Se le puede pedir el cierre: el bot corre y nadie lo pidió ya."""
        return not self.task.done() and not self.bot.snapshot()["close_requested"]

    @property
    def active(self) -> bool:
        """Sigue "ocupando" su mint: el bot corre, o terminó dejando la
        posición abierta (venta de cierre fallida -> los tokens siguen en
        la wallet y hay que revisarlo a mano)."""
        if not self.task.done():
            return True
        pos = self.bot.position
        return pos is not None and not pos.closed


PositionRef = Union[int, str]

# Lo que cuesta una compra real además de buy_sol: el rent de crear la
# cuenta del token (~0.00204 SOL), las comisiones de la transacción y de
# PumpPortal. El priority fee se suma aparte (es configurable).
BUY_OVERHEAD_SOL = 0.004
# El saldo de la wallet se recuerda unos segundos (la web lo pide a menudo).
WALLET_CACHE_SECONDS = 20.0
# Tras vender, cuánto tiempo más se sigue registrando el precio (ver
# TrailingTakeProfitBot.follow_after_close).
FOLLOW_AFTER_CLOSE_SECONDS = 30 * 60


class PositionManager:
    FOLLOW_AFTER_CLOSE_SECONDS = FOLLOW_AFTER_CLOSE_SECONDS

    def __init__(self, base_config: AppConfig, client, feed: Optional[SharedTradeFeed] = None,
                 metadata_client=None, panel_factory: Optional[Callable[["ManagedPosition"], object]] = None,
                 executor_factory: Optional[Callable[[AppConfig], object]] = None,
                 store: Optional[PositionStore] = None, jupiter=None):
        """`client` es el PumpPortalClient real (trades de la Lightning API,
        balances); el precio llega por `feed` (uno compartido sobre ese
        mismo cliente si no se pasa). `panel_factory(managed)` devuelve el
        panel de cada bot (None = sin panel: el estado se loguea).
        `executor_factory` existe para los tests. `store` guarda las
        posiciones para retomarlas (None = sin persistencia). `jupiter` es el
        JupiterClient (None = sin Jupiter): lo comparten todas las
        posiciones, con un único cupo de peticiones."""
        self.base_config = base_config
        self.client = client
        self.feed = feed if feed is not None else SharedTradeFeed(client)
        self.metadata_client = metadata_client
        self.panel_factory = panel_factory
        self.jupiter = jupiter
        self.executor_factory = executor_factory or (
            lambda cfg: TradeExecutor(client=client, live=cfg.live, config=cfg, jupiter=jupiter))
        self.store = store
        self._positions: Dict[int, ManagedPosition] = {}
        self._next_id = 1  # solo sin store; con store, el id lo da la base de datos
        self._closing_all = False
        self._changed = asyncio.Event()
        self._wallet_cache: Optional[tuple] = None  # (monotonic, address, sol)
        self._wallet_mismatch_warned = False
        # Tareas que siguen registrando el precio de posiciones ya vendidas.
        self._followers: set = set()

    # --- control ------------------------------------------------------------ #
    def open(self, mint: str, **overrides) -> ManagedPosition:
        """Arranca una posición nueva para `mint` y devuelve enseguida (la
        compra ocurre en segundo plano, cuando el bot consiga precio).

        Lanza ValueError si el mint no es válido, si ya hay una posición
        activa de ese mint, si se llegó a max_open_positions, si un ajuste
        no se puede cambiar por posición o deja la config inválida (p. ej.
        buy_sol por encima de max_buy_sol), o si se está cerrando todo."""
        if self._closing_all:
            raise ValueError("Se están cerrando todas las posiciones; no se abren nuevas.")
        activas = self.active_count()
        if activas >= self.base_config.max_open_positions:
            raise ValueError(f"Ya hay {activas} posiciones activas, el máximo "
                             f"(general.max_open_positions = {self.base_config.max_open_positions}). "
                             f"Cierra alguna o sube el límite en el .toml.")
        mint = validate_mint(mint)
        ocupada = self._active_for_mint(mint)
        if ocupada is not None:
            raise ValueError(f"Ya hay una posición activa de {mint} (#{ocupada.id}). Comparten wallet, "
                             f"así que no se pueden tener dos del mismo token a la vez.")
        desconocidos = set(overrides) - set(OVERRIDABLE_FIELDS)
        if desconocidos:
            raise ValueError(f"No se puede ajustar por posición: {sorted(desconocidos)}. "
                             f"Permitidos: {list(OVERRIDABLE_FIELDS)}")

        cfg = dataclasses.replace(self.base_config, mint=mint, **overrides)
        validate_config(cfg)

        if self.store is not None:
            pos_id = self.store.create(mint, cfg.live, {k: getattr(cfg, k) for k in OVERRIDABLE_FIELDS})
        else:
            pos_id = self._next_id
            self._next_id += 1
        managed = self._start(pos_id, mint, cfg)
        logger.info(f"Posición #{pos_id} abierta para {mint} "
                    f"(compra {cfg.buy_sol} SOL, modo {'REAL' if cfg.live else 'SIMULADO'}).")
        return managed

    def _start(self, pos_id: int, mint: str, cfg: AppConfig, resume_position: Optional[Position] = None,
               resume_onchain_source: Optional[str] = None) -> ManagedPosition:
        bot = TrailingTakeProfitBot(client=self.feed, executor=self.executor_factory(cfg), config=cfg,
                                    metadata_client=self.metadata_client,
                                    resume_position=resume_position,
                                    resume_onchain_source=resume_onchain_source,
                                    jupiter=self.jupiter)
        task = asyncio.create_task(bot.run(), name=f"pepump-pos-{pos_id}")
        managed = ManagedPosition(id=pos_id, mint=mint, config=cfg, bot=bot, task=task)
        if self.store is not None:
            bot._persist_cb = lambda b, m=managed: self._save(m)
            # Al retomar, el gráfico sigue desde donde estaba.
            bot.price_history.extend(self.store.prices(pos_id))
            bot._price_cb = lambda ts, price, pid=pos_id: self.store.add_price(pid, ts, price)
        # El panel se engancha después de crear el bot porque necesita el
        # ManagedPosition (id) para dibujarse; el bot todavía no corrió
        # ni una línea, así que no se pierde nada.
        if self.panel_factory is not None:
            bot.panel = self.panel_factory(managed)
            bot._update_panel_fixed()
        task.add_done_callback(functools.partial(self._on_done, managed))
        self._positions[pos_id] = managed
        if len(self._positions) > 1:
            enable_tags()
        self._notify()
        return managed

    def close(self, ref: PositionRef, reason: str = "cierre manual") -> ManagedPosition:
        """Pide el cierre ordenado de una posición (por id o por mint). No
        espera a que se venda: para eso, `await managed.task`."""
        managed = self.get(ref)
        if managed is None:
            raise ValueError(f"No existe la posición {ref!r}.")
        managed.bot.close(reason)
        self._notify()
        return managed

    def close_all(self, reason: str = "cierre de todas las posiciones", block_new: bool = True) -> int:
        """Pide el cierre de todas las posiciones que siguen corriendo y
        devuelve cuántas eran. `block_new` impide abrir posiciones nuevas
        desde ese momento: es lo que corresponde al apagar el proceso
        (Ctrl+C), no a un "cerrar todas" desde la web, tras el que el
        servicio sigue vivo."""
        if block_new:
            self._closing_all = True
            self._cancel_followers()
        pedidas = 0
        for managed in list(self._positions.values()):
            if managed.closable:
                managed.bot.close(reason)
                pedidas += 1
        self._notify()
        return pedidas

    def _cancel_followers(self) -> None:
        for task in self._followers:
            task.cancel()

    def stop_all(self, reason: str = "bot detenido") -> int:
        """Detiene todos los bots SIN vender (lo que hace Ctrl+C por
        defecto): las posiciones abiertas quedan guardadas y se retoman al
        volver a arrancar; las que esperaban entrada se cancelan. No se
        abren posiciones nuevas desde ese momento."""
        self._closing_all = True
        self._cancel_followers()
        pedidas = 0
        for managed in list(self._positions.values()):
            if managed.closable:
                managed.bot.stop(reason)
                pedidas += 1
        self._notify()
        return pedidas

    async def wait_all(self) -> None:
        """Espera a que terminen TODOS los bots, incluidos los que se abran
        mientras tanto."""
        while True:
            pendientes = [m.task for m in self._positions.values() if not m.task.done()]
            if not pendientes:
                return
            await asyncio.gather(*pendientes, return_exceptions=True)

    async def aclose(self) -> None:
        """Al apagar el proceso, después de wait_all(): corta el registro de
        precios tras la venta y cierra el feed."""
        self._cancel_followers()
        if self._followers:
            await asyncio.gather(*self._followers, return_exceptions=True)
        await self.feed.close()

    # --- persistencia -------------------------------------------------------- #
    def _save(self, managed: ManagedPosition) -> None:
        if self.store is None:
            return
        bot = managed.bot
        pos = bot.position
        if pos is not None and pos.closed:
            status = "closed"
        elif pos is not None:
            status = "open"
        elif bot.phase == "buying":
            status = "buying"
        elif bot.running:
            status = "waiting_entry"
        else:
            status = "cancelled"
        fields: Dict[str, object] = {
            "status": status,
            "token_name": bot.token_name,
            "token_symbol": bot.token_symbol,
            "onchain_source": bot.onchain.source,
            "end_reason": bot.end_reason,
        }
        if pos is not None:
            fields.update(
                entry_price=pos.entry_price, market_entry_price=pos.market_entry_price,
                sol_amount=pos.sol_amount, token_amount=pos.token_amount,
                entry_is_real_fill=int(pos.entry_is_real_fill), opened_at=pos.opened_at,
                armed=int(pos.armed), armed_at=pos.armed_at, highest_price=pos.highest_price,
                closed_at=pos.closed_at, exit_price=pos.exit_price, proceeds_sol=pos.proceeds_sol,
                realized_pnl_sol=pos.realized_pnl_sol, realized_pnl_pct=pos.realized_pnl_pct,
                close_reason=pos.close_reason, exit_is_real_fill=int(pos.exit_is_real_fill),
            )
        self.store.update(managed.id, fields)
        # La wallet solo se conoce tras la primera compra confirmada (ver
        # PumpPortalClient.wallet_address): se guarda para poder mirar los
        # balances al retomar después de un reinicio.
        wallet = getattr(self.client, "wallet_address", None)
        if wallet and self.store.get_meta("wallet_address") != wallet:
            self.store.set_meta("wallet_address", wallet)
        self._check_wallet_mismatch(wallet)

    def _check_wallet_mismatch(self, learned: Optional[str]) -> None:
        configured = self.base_config.wallet_address
        if configured and learned and learned != configured and not self._wallet_mismatch_warned:
            self._wallet_mismatch_warned = True
            logger.error(f"⚠️  pumpportal.wallet_address ({configured}) NO es la wallet que usa tu api_key "
                         f"({learned}, la que pagó la última compra). El saldo que muestra la web es de "
                         f"la otra: corrige wallet_address en el .toml.")

    # --- wallet ------------------------------------------------------------- #
    def wallet_address(self) -> Optional[str]:
        """La de pumpportal.wallet_address; si no, la aprendida de la primera
        compra real."""
        return self.base_config.wallet_address or getattr(self.client, "wallet_address", None)

    async def wallet_info(self, max_age: float = WALLET_CACHE_SECONDS) -> dict:
        address = self.wallet_address()
        info = {"address": address, "sol": None,
                "source": "config" if self.base_config.wallet_address else ("learned" if address else None)}
        if not address or self.client is None:
            return info
        cached = self._wallet_cache
        if cached and cached[1] == address and time.monotonic() - cached[0] < max_age:
            info["sol"] = cached[2]
            return info
        sol = await self.client.get_sol_balance(self.base_config.solana_rpc_url, address)
        if sol is not None:
            self._wallet_cache = (time.monotonic(), address, sol)
        info["sol"] = sol
        return info

    async def check_funds(self, buy_sol: float, priority_fee: float) -> None:
        """Antes de una compra REAL: ValueError si la wallet no tiene SOL para
        buy_sol + priority fee + BUY_OVERHEAD_SOL. Saldo fresco (sin caché). Si
        no se conoce la wallet o el RPC no responde, no bloquea: la compra
        fallaría igual en la Lightning API, con su propio error."""
        info = await self.wallet_info(max_age=0)
        if info["sol"] is None:
            return
        needed = buy_sol + priority_fee + BUY_OVERHEAD_SOL
        if info["sol"] < needed:
            raise ValueError(f"Saldo insuficiente: la wallet tiene {info['sol']:.4f} SOL y esta compra "
                             f"necesita unos {needed:.4f} SOL ({buy_sol} + priority fee + "
                             f"~{BUY_OVERHEAD_SOL} de comisiones y cuenta del token).")

    async def recover(self) -> List[ManagedPosition]:
        """Al arrancar: retoma las posiciones que quedaron abiertas en la
        ejecución anterior y limpia las que se quedaron a medias. Devuelve
        las retomadas. Sin store, no hace nada."""
        if self.store is None:
            return []
        rpc = self.base_config.solana_rpc_url
        wallet = self.store.get_meta("wallet_address")
        if wallet and self.client is not None and not getattr(self.client, "wallet_address", None):
            self.client.wallet_address = wallet

        for row in self.store.with_status("waiting_entry"):
            self.store.update(row["id"], {"status": "cancelled",
                                          "end_reason": "el bot se detuvo antes de comprar"})

        retomadas = []
        for row in self.store.with_status("buying", "open"):
            pos_id, mint = row["id"], row["mint"]
            # Cada posición se retoma en SU modo (real o simulada), sea cual
            # sea general.live ahora.
            live = row["live"]
            if self._active_for_mint(mint) is not None:
                logger.error(f"Posición #{pos_id} ({mint}): ya hay otra activa de ese mint; no se retoma.")
                continue

            balance = None
            if live:
                balance = await self.client.get_wallet_token_balance(mint, rpc)

            if row["status"] == "buying":
                self._recover_half_buy(row, balance)
                continue

            if balance is not None and balance <= 0:
                motivo = "ya no hay tokens en la wallet (vendida fuera del bot)"
                logger.warning(f"Posición #{pos_id} ({mint}): {motivo}. Se marca como cerrada.")
                self.store.update(pos_id, {"status": "closed", "closed_at": time.time(),
                                           "close_reason": motivo, "end_reason": motivo})
                continue
            if live and balance is None:
                logger.warning(f"Posición #{pos_id} ({mint}): no se pudo comprobar la wallet "
                               f"(¿RPC caído?). Se retoma igualmente.")
            elif balance is not None and row["token_amount"] and balance < row["token_amount"] * 0.99:
                logger.warning(f"Posición #{pos_id} ({mint}): en la wallet hay {balance:,.2f} tokens y la "
                               f"posición tenía {row['token_amount']:,.2f}. Al vender se vende TODO lo "
                               f"que haya de ese token.")

            settings = {k: v for k, v in row["settings"].items() if k in OVERRIDABLE_FIELDS}
            settings["live"] = live
            cfg = dataclasses.replace(self.base_config, mint=mint, **settings)
            position = Position(mint=mint, entry_price=row["entry_price"], sol_amount=row["sol_amount"],
                                token_amount=row["token_amount"], market_entry_price=row["market_entry_price"],
                                entry_is_real_fill=row["entry_is_real_fill"], opened_at=row["opened_at"])
            position.armed = row["armed"]
            position.armed_at = row.get("armed_at")
            if row["highest_price"]:
                position.highest_price = row["highest_price"]
            managed = self._start(pos_id, mint, cfg, resume_position=position,
                                  resume_onchain_source=row["onchain_source"])
            managed.bot.token_name, managed.bot.token_symbol = row["token_name"], row["token_symbol"]
            retomadas.append(managed)
            logger.info(f"♻️  Posición #{pos_id} ({mint}) retomada.")
        return retomadas

    def _recover_half_buy(self, row: dict, balance: Optional[float]) -> None:
        """El proceso murió con una compra enviada y sin confirmar."""
        assert self.store is not None  # solo se llama desde recover()
        pos_id, mint = row["id"], row["mint"]
        if not row["live"]:
            motivo = "el bot se detuvo durante la compra (simulada)"
            self.store.update(pos_id, {"status": "cancelled", "end_reason": motivo})
            return
        if balance is not None and balance <= 0:
            motivo = "la compra no llegó a ejecutarse (no hay tokens en la wallet)"
            logger.info(f"Posición #{pos_id} ({mint}): {motivo}.")
            self.store.update(pos_id, {"status": "cancelled", "end_reason": motivo})
            return
        if balance is None:
            motivo = ("el bot se detuvo durante la compra y no se pudo comprobar la wallet: "
                      "revisa a mano si se compró")
        else:
            motivo = (f"el bot se detuvo durante la compra y en la wallet hay {balance:,.2f} tokens, "
                      f"pero no hay datos de entrada: revísala y véndela a mano")
        logger.error(f"⚠️  Posición #{pos_id} ({mint}): {motivo}. https://pump.fun/{mint}")
        self.store.update(pos_id, {"status": "review", "end_reason": motivo})

    def history(self, limit: int = 100) -> List[dict]:
        """Posiciones terminadas (de esta ejecución y de las anteriores), las
        más recientes primero. Las que siguen activas no salen aquí."""
        if self.store is None:
            return []
        activas = tuple(m.id for m in self._positions.values() if m.active)
        return self.store.history(limit=limit, exclude_ids=activas)

    def history_summary(self) -> dict:
        """PnL de las posiciones cerradas, reales y simuladas por separado
        (ver PositionStore.history_summary)."""
        if self.store is None:
            return {"real": None, "sim": None}
        return self.store.history_summary()

    def clear_history(self, live: Optional[bool] = None) -> int:
        """Borra del historial las posiciones terminadas (todas, solo las
        reales o solo las simuladas) y devuelve cuántas. También las quita
        de esta sesión -para que no cuenten en el PnL realizado del panel- y
        corta el registro de precios tras la venta de las borradas, que si
        no seguiría escribiendo puntos de una posición que ya no existe.
        El .csv de órdenes cerradas no se toca."""
        if self.store is None:
            return 0
        activas = tuple(m.id for m in self._positions.values() if m.active)
        borradas = set(self.store.clear_history(live=live, exclude_ids=activas))
        nombres = {f"pepump-follow-{pos_id}" for pos_id in borradas}
        for task in self._followers:
            if task.get_name() in nombres:
                task.cancel()
        for pos_id in borradas:
            self._positions.pop(pos_id, None)
        if borradas:
            logger.info(f"Historial: {len(borradas)} posiciones terminadas borradas.")
            self._notify()
        return len(borradas)

    # --- consulta ----------------------------------------------------------- #
    def get(self, ref: PositionRef) -> Optional[ManagedPosition]:
        if isinstance(ref, int):
            return self._positions.get(ref)
        ref = str(ref).strip()
        if ref.isdigit():
            return self._positions.get(int(ref))
        # Por mint: la más reciente de ese mint.
        candidatas = [m for m in self._positions.values() if m.mint == ref]
        return candidatas[-1] if candidatas else None

    def positions(self) -> List[ManagedPosition]:
        return list(self._positions.values())

    def active_count(self) -> int:
        return sum(1 for m in self._positions.values() if m.active)

    def defaults(self) -> dict:
        """Lo que necesita el formulario de "nueva posición": los ajustes
        por defecto (del .toml) y los límites."""
        cfg = self.base_config
        return {
            "live": cfg.live,
            "settings": {k: getattr(cfg, k) for k in OVERRIDABLE_FIELDS},
            "max_buy_sol": cfg.max_buy_sol,
            "max_open_positions": cfg.max_open_positions,
            "active_positions": self.active_count(),
            "accepting_new": not self._closing_all,
        }

    async def preview(self, mint: str) -> dict:
        """Qué es este mint, ANTES de comprar: nombre/símbolo, dónde cotiza
        y a qué precio. Solo consultas baratas (getAccountInfo /
        getMultipleAccounts). Lanza ValueError si el mint no es válido."""
        mint = validate_mint(mint)
        rpc = self.base_config.solana_rpc_url
        out: Dict[str, object] = {"mint": mint, "name": None, "symbol": None, "market": None, "price": None,
               "active_position": None}
        ocupada = self._active_for_mint(mint)
        if ocupada is not None:
            out["active_position"] = ocupada.id

        async def nombre():
            if self.metadata_client is None:
                return None
            try:
                return await self.metadata_client.fetch_name_symbol(mint)
            except Exception:
                return None

        async def mercado():
            price, complete, exists = await PumpCurveOnChainClient(rpc).fetch_price_or_status(mint)
            if exists and not complete and price:
                return "bondingcurve", price
            price, absent = await PumpSwapOnChainClient(rpc).fetch_price_or_confirm_absent(mint)
            if price:
                return "pumpswap", price
            if exists is False and absent:
                # Ni curva de pump.fun ni pool de PumpSwap: el bot todavía
                # puede intentar Raydium CPMM / Meteora DLMM al entrar, y si
                # no, Jupiter (la cotización es orientativa: al entrar se
                # mira primero lo demás).
                if self.jupiter is not None:
                    try:
                        tokens = await self.jupiter.buy_tokens(mint, self.base_config.buy_sol, rpc,
                                                               priority=PRIORITY_BACKGROUND)
                        return "jupiter", await self.jupiter.sell_price(mint, tokens, rpc,
                                                                        priority=PRIORITY_BACKGROUND)
                    except Exception as e:
                        logger.debug(f"[Vista previa] Jupiter no cotiza {mint}: {e}")
                return "other", None
            return "unknown", None

        found, (market, price) = await asyncio.gather(nombre(), mercado())
        if found:
            out["name"], out["symbol"] = found
        out["market"], out["price"] = market, price
        return out

    def snapshot(self) -> List[dict]:
        return [{"id": m.id, **m.bot.snapshot()} for m in self._positions.values()]

    async def wait_changed(self) -> None:
        """Se despierta cuando se abre, se pide cerrar o termina una
        posición (no con cada cambio de precio: para eso, snapshot())."""
        await self._changed.wait()

    # --- internos ----------------------------------------------------------- #
    def _active_for_mint(self, mint: str) -> Optional[ManagedPosition]:
        for managed in self._positions.values():
            if managed.mint == mint and managed.active:
                return managed
        return None

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _on_done(self, managed: ManagedPosition, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error(f"La posición #{managed.id} ({managed.mint}) terminó con un error "
                         f"inesperado: {task.exception()!r}")
        if managed.active and not managed.bot.detached:
            logger.error(f"⚠️  La posición #{managed.id} ({managed.mint}) terminó con la posición "
                         f"ABIERTA: los tokens siguen en la wallet. Revísala a mano: "
                         f"https://pump.fun/{managed.mint}")
        self._maybe_follow(managed)
        self._notify()

    def _maybe_follow(self, managed: ManagedPosition) -> None:
        """Tras una venta, sigue registrando el precio un rato (ver
        follow_after_close). Solo con base de datos, que es donde sirve, y
        no si el proceso se está apagando."""
        pos = managed.bot.position
        if self.store is None or self._closing_all or pos is None or not pos.closed:
            return
        task = asyncio.create_task(managed.bot.follow_after_close(self.FOLLOW_AFTER_CLOSE_SECONDS),
                                   name=f"pepump-follow-{managed.id}")
        self._followers.add(task)
        task.add_done_callback(self._followers.discard)
