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
  - los cierra de forma ordenada (close / close_all -> bot.close()),
  - y da una foto de todas (snapshot) para mostrarlas.
"""
import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

from pepump.bot import TrailingTakeProfitBot
from pepump.config import AppConfig, validate_config, validate_mint
from pepump.executor import TradeExecutor
from pepump.feed import SharedTradeFeed

logger = logging.getLogger(__name__)

# Lo que se puede ajustar por posición al abrirla. El resto (RPC, api_key,
# modo real/simulado, timeouts) es del proceso y sale siempre del .toml.
OVERRIDABLE_FIELDS = (
    "buy_sol", "slippage", "priority_fee", "pool",
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


class PositionManager:
    def __init__(self, base_config: AppConfig, client, feed: Optional[SharedTradeFeed] = None,
                 metadata_client=None, panel_factory: Optional[Callable[["ManagedPosition"], object]] = None,
                 executor_factory: Optional[Callable[[AppConfig], object]] = None):
        """`client` es el PumpPortalClient real (trades de la Lightning API,
        balances); el precio llega por `feed` (uno compartido sobre ese
        mismo cliente si no se pasa). `panel_factory(managed)` devuelve el
        panel de cada bot (None = sin panel: el estado se loguea).
        `executor_factory` existe para los tests."""
        self.base_config = base_config
        self.client = client
        self.feed = feed if feed is not None else SharedTradeFeed(client)
        self.metadata_client = metadata_client
        self.panel_factory = panel_factory
        self.executor_factory = executor_factory or (
            lambda cfg: TradeExecutor(client=client, live=cfg.live, config=cfg))
        self._positions: Dict[int, ManagedPosition] = {}
        self._next_id = 1
        self._closing_all = False
        self._changed = asyncio.Event()

    # --- control ------------------------------------------------------------ #
    def open(self, mint: str, **overrides) -> ManagedPosition:
        """Arranca una posición nueva para `mint` y devuelve enseguida (la
        compra ocurre en segundo plano, cuando el bot consiga precio).

        Lanza ValueError si el mint no es válido, si ya hay una posición
        activa de ese mint, si un ajuste no se puede cambiar por posición o
        deja la config inválida, o si se está cerrando todo."""
        if self._closing_all:
            raise ValueError("Se están cerrando todas las posiciones; no se abren nuevas.")
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

        pos_id = self._next_id
        self._next_id += 1
        bot = TrailingTakeProfitBot(client=self.feed, executor=self.executor_factory(cfg), config=cfg,
                                    metadata_client=self.metadata_client)
        task = asyncio.create_task(bot.run(), name=f"pepump-pos-{pos_id}")
        managed = ManagedPosition(id=pos_id, mint=mint, config=cfg, bot=bot, task=task)
        # El panel se engancha después de crear el bot porque necesita el
        # ManagedPosition (id) para dibujarse; el bot todavía no corrió
        # ni una línea, así que no se pierde nada.
        if self.panel_factory is not None:
            bot.panel = self.panel_factory(managed)
            bot._update_panel_fixed()
        task.add_done_callback(lambda t, m=managed: self._on_done(m, t))
        self._positions[pos_id] = managed
        self._notify()
        logger.info(f"Posición #{pos_id} abierta para {mint} "
                    f"(compra {cfg.buy_sol} SOL, modo {'REAL' if cfg.live else 'SIMULADO'}).")
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
        pedidas = 0
        for managed in list(self._positions.values()):
            if managed.closable:
                managed.bot.close(reason)
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
        """Al apagar el proceso, después de wait_all(): cierra el feed."""
        await self.feed.close()

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
        if managed.active:
            logger.error(f"⚠️  La posición #{managed.id} ({managed.mint}) terminó con la posición "
                         f"ABIERTA: los tokens siguen en la wallet. Revísala a mano: "
                         f"https://pump.fun/{managed.mint}")
        self._notify()
