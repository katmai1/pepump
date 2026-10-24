"""
Persistencia (store.py) y recuperación al arrancar (PositionManager.recover):
parar sin vender, retomar donde estaba, y los casos raros.
"""
import asyncio

import pytest

from pepump.feed import SharedTradeFeed
from pepump.manager import PositionManager
from pepump.pumpportal import TradeOutcomeUnknown
from pepump.store import PositionStore

from .conftest import SpyExecutor, make_config
from .test_shared_feed import Conexiones, FakeClient

A = "So11111111111111111111111111111111111111112"
B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class WalletClient(FakeClient):
    """Cliente falso con wallet: `balances[mint]` (None = no se pudo consultar)."""

    def __init__(self, balances=None, wallet_address=None):
        self.balances = balances or {}
        self.wallet_address = wallet_address

    async def get_wallet_token_balance(self, mint, rpc_url):
        return self.balances.get(mint)


def make_manager(store, client=None, **cfg):
    conns = Conexiones()
    client = client or WalletClient()
    feed = SharedTradeFeed(client, connect=conns)
    executors = []

    def executor_factory(c):
        executors.append(SpyExecutor())
        return executors[-1]

    base = make_config(status_interval_seconds=999, activation_pct=20.0, trailing_pct=10.0,
                       initial_stop_pct=25.0, strategy_price_basis="market", **cfg)
    manager = PositionManager(base_config=base, client=client, feed=feed, store=store,
                              executor_factory=executor_factory)
    return manager, conns, executors


async def _hasta(cond, timeout=2.0):
    async def _loop():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(_loop(), timeout)


async def _precio(manager, conns, mint, price):
    await _hasta(lambda: conns.sockets and mint in manager.feed.active_mints)
    conns.sockets[-1].push({"mint": mint, "price": price})


def test_store_basico(tmp_path):
    path = str(tmp_path / "x.db")
    s = PositionStore(path)
    pos_id = s.create(A, live=False, settings={"buy_sol": 0.1})
    s.update(pos_id, {"status": "open", "entry_price": 1.5, "armed": 1})
    s.set_meta("wallet_address", "W")
    s.close()

    s = PositionStore(path)  # sobrevive a cerrar y reabrir
    row = s.get(pos_id)
    assert row["status"] == "open" and row["entry_price"] == 1.5 and row["armed"] is True
    assert row["settings"] == {"buy_sol": 0.1}
    assert s.get_meta("wallet_address") == "W"
    with pytest.raises(ValueError):
        s.update(pos_id, {"no_existe": 1})
    s.close()


async def test_guarda_compra_maximo_y_cierre(store):
    manager, conns, executors = make_manager(store)
    managed = manager.open(A, buy_sol=0.2)
    assert store.get(managed.id)["status"] == "waiting_entry"
    assert store.get(managed.id)["settings"]["buy_sol"] == 0.2

    await _precio(manager, conns, A, 1.0)
    await _hasta(lambda: store.get(managed.id)["status"] == "open")
    row = store.get(managed.id)
    assert row["market_entry_price"] == 1.0 and row["armed"] is False

    await _precio(manager, conns, A, 1.3)   # arma (+30% > 20%)
    await _precio(manager, conns, A, 1.5)   # nuevo máximo
    await _hasta(lambda: store.get(managed.id)["highest_price"] == 1.5)
    assert store.get(managed.id)["armed"] is True

    await _precio(manager, conns, A, 1.3)   # -13% desde el máximo: vende
    await asyncio.wait_for(managed.task, 5)
    row = store.get(managed.id)
    assert row["status"] == "closed"
    assert "retroceso" in row["close_reason"]
    await manager.aclose()


async def test_parar_sin_vender_y_retomar_donde_estaba(store):
    # --- primera ejecución: compra, arma, Ctrl+C (sin vender) ---
    manager, conns, executors = make_manager(store)
    managed = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await _precio(manager, conns, A, 1.4)
    await _hasta(lambda: store.get(managed.id)["armed"])
    manager.stop_all("Ctrl+C")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()

    assert executors[0].sell_calls == 0
    assert managed.bot.detached
    assert managed.bot.snapshot()["phase"] == "open"
    row = store.get(managed.id)
    assert row["status"] == "open" and row["highest_price"] == 1.4

    # --- segunda ejecución: retoma, SIN comprar, con el máximo de antes ---
    manager2, conns2, executors2 = make_manager(store)
    retomadas = await manager2.recover()
    assert [m.id for m in retomadas] == [managed.id]
    bot = retomadas[0].bot
    assert bot.recovered and bot.position.armed and bot.position.highest_price == 1.4
    assert bot.snapshot()["recovered"] is True

    # 1.25 está por debajo del nivel de venta (1.4 * 0.9 = 1.26): vende ya.
    await _precio(manager2, conns2, A, 1.25)
    await asyncio.wait_for(retomadas[0].task, 5)
    assert executors2[0].buy_calls == 0
    assert executors2[0].sell_calls == 1
    assert store.get(managed.id)["status"] == "closed"
    await manager2.aclose()


async def test_parar_mientras_espera_entrada_cancela(store):
    manager, conns, executors = make_manager(store)
    managed = manager.open(A)
    await _hasta(lambda: managed.bot.phase == "waiting_entry")
    manager.stop_all("Ctrl+C")
    await asyncio.wait_for(manager.wait_all(), 5)
    assert store.get(managed.id)["status"] == "cancelled"

    manager2, _, _ = make_manager(store)
    assert await manager2.recover() == []


async def test_cerrar_despues_de_parar_vende():
    """'Cerrar' en la web mientras el proceso se detiene: gana vender."""
    manager, conns, executors = make_manager(None)
    managed = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await _hasta(lambda: managed.bot.position is not None)
    managed.bot.stop("Ctrl+C")
    managed.bot.close("cerrada desde la web")
    await asyncio.wait_for(managed.task, 5)
    assert executors[0].sell_calls == 1
    assert not managed.bot.detached


async def test_parando_sin_vender_la_web_todavia_puede_cerrar():
    """bot.close() sabía pasar de "parar" a "vender", pero closable daba
    False en cuanto había un cierre pedido: la web respondía 409 y nunca se
    llegaba ahí."""
    manager, conns, executors = make_manager(None)
    managed = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await _hasta(lambda: managed.bot.position is not None)
    managed.bot.stop("Ctrl+C")
    assert managed.closable and managed.bot.snapshot()["stopping_without_selling"]
    manager.close(managed.id, "cerrada desde la web")
    assert not managed.closable  # ya va a vender: un segundo cierre no hace nada
    await asyncio.wait_for(managed.task, 5)
    assert executors[0].sell_calls == 1


async def test_al_arrancar_lo_que_esperaba_entrada_se_cancela(store):
    pos_id = store.create(A, live=False, settings={})
    manager, _, _ = make_manager(store)
    assert await manager.recover() == []
    assert store.get(pos_id)["status"] == "cancelled"


def _abierta(store, mint, live, **extra):
    pos_id = store.create(mint, live=live, settings={"buy_sol": 0.05})
    store.update(pos_id, {"status": "open", "entry_price": 1.0, "market_entry_price": 1.0,
                          "sol_amount": 0.05, "token_amount": 0.05, "opened_at": 1.0,
                          "highest_price": 1.0, **extra})
    return pos_id


async def test_real_tokens_vendidos_fuera_del_bot(store):
    pos_id = _abierta(store, A, live=True)
    store.set_meta("wallet_address", "MiWallet")
    client = WalletClient(balances={A: 0.0})
    manager, _, _ = make_manager(store, client=client, live=True)
    assert await manager.recover() == []
    assert client.wallet_address == "MiWallet"  # recuperada de la base de datos
    row = store.get(pos_id)
    assert row["status"] == "closed" and "fuera del bot" in row["close_reason"]


async def test_real_con_tokens_se_retoma(store):
    pos_id = _abierta(store, A, live=True)
    manager, _, _ = make_manager(store, client=WalletClient(balances={A: 0.05}, wallet_address="W"),
                                 live=True)
    retomadas = await manager.recover()
    assert [m.id for m in retomadas] == [pos_id]
    manager.stop_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()


async def test_cada_posicion_se_retoma_en_su_modo(store):
    """El modo es de cada posición: una REAL se retoma como real aunque
    general.live diga simulado ahora (y al revés)."""
    real = _abierta(store, A, live=True)
    simulada = _abierta(store, B, live=False)
    client = WalletClient(balances={A: 0.05}, wallet_address="W")
    manager, _, _ = make_manager(store, client=client)  # general.live = False
    retomadas = {m.id: m for m in await manager.recover()}
    assert retomadas[real].config.live is True
    assert retomadas[real].bot.snapshot()["live"] is True
    assert retomadas[simulada].config.live is False
    manager.stop_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()


@pytest.mark.parametrize("live,balance,esperado", [
    (False, None, "cancelled"),   # simulado: no había dinero
    (True, 0.0, "cancelled"),     # real, la compra no entró
    (True, 123.0, "review"),      # real, hay tokens sin datos de entrada
    (True, None, "review"),       # real, no se pudo mirar la wallet
])
async def test_compra_a_medias(store, live, balance, esperado):
    pos_id = store.create(A, live=live, settings={})
    store.update(pos_id, {"status": "buying"})
    client = WalletClient(balances={A: balance}, wallet_address="W")
    manager, _, executors = make_manager(store, client=client, live=live)
    assert await manager.recover() == []
    assert store.get(pos_id)["status"] == esperado
    assert executors == []  # nunca se arranca un bot para esto


@pytest.mark.parametrize("exc, esperado", [
    (TradeOutcomeUnknown("read timeout"), "review"),    # no se sabe si entró
    (RuntimeError("slippage excedido"), "cancelled"),   # seguro que no entró
])
async def test_compra_sin_resultado_conocido_queda_para_revisar(store, exc, esperado):
    """Antes una compra que no se sabía si había entrado acababa como
    'cancelled': nadie miraba la wallet ni al reiniciar, y si la tx entraba
    los tokens se quedaban sin vigilar."""
    class CompraQueFalla(SpyExecutor):
        async def buy(self, mint, price, pool_override=None):
            raise exc

    manager, conns, _ = make_manager(store, live=True)
    manager.executor_factory = lambda cfg: CompraQueFalla()
    managed = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await asyncio.wait_for(managed.task, 5)
    assert store.get(managed.id)["status"] == esperado
    await manager.aclose()


async def test_active_mints_son_los_de_posiciones_activas(store):
    manager, conns, _ = make_manager(store)
    managed = manager.open(A)
    assert manager.active_mints() == {A}
    manager.stop_all("fin")
    await asyncio.wait_for(managed.task, 5)
    assert manager.active_mints() == set()
    await manager.aclose()


async def test_retomar_con_mas_tokens_en_la_wallet_avisa(store, caplog):
    pos_id = _abierta(store, A, live=True)
    client = WalletClient(balances={A: 999_999.0}, wallet_address="W")
    manager, _, _ = make_manager(store, client=client)
    await manager.recover()
    assert "el PnL de la posición cuenta solo los suyos" in caplog.text
    manager.stop_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()
    assert store.get(pos_id)["status"] == "open"


async def test_al_retomar_el_nombre_guardado_sale_limpio(store):
    pos_id = _abierta(store, A, live=False, token_name="Pepe\x1b[2J", token_symbol="PE\nPE")
    manager, _, _ = make_manager(store)
    (managed,) = await manager.recover()
    assert (managed.bot.token_name, managed.bot.token_symbol) == ("Pepe[2J", "PEPE")
    manager.stop_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()
    assert pos_id == managed.id


async def test_ids_persistentes_e_historial(store):
    manager, conns, _ = make_manager(store)
    primera = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await _hasta(lambda: primera.bot.position is not None)
    manager.close(primera.id, "test")
    await asyncio.wait_for(primera.task, 5)
    await manager.aclose()

    manager2, _, _ = make_manager(store)
    segunda = manager2.open(B)
    assert segunda.id > primera.id  # no se reutilizan ids entre ejecuciones
    hist = manager2.history()
    assert [h["id"] for h in hist] == [primera.id]
    manager2.stop_all("fin")
    await asyncio.wait_for(manager2.wait_all(), 5)
    await manager2.aclose()


async def test_precios_y_armado_se_guardan_y_vuelven_al_retomar(store):
    manager, conns, _ = make_manager(store)
    managed = manager.open(A)
    for p in (1.0, 1.3):  # compra y arma (+30 %, salto > 3 %: se guardan los dos)
        await _precio(manager, conns, A, p)
        await asyncio.sleep(0.05)
    await _hasta(lambda: managed.bot.position is not None and managed.bot.position.armed)
    manager.stop_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()

    assert [p for _ts, p in store.prices(managed.id)] == [1.0, 1.3]
    armed_at = store.get(managed.id)["armed_at"]
    assert armed_at is not None

    manager2, _, _ = make_manager(store)
    (retomada,) = await manager2.recover()
    assert [p for _ts, p in retomada.bot.price_history] == [1.0, 1.3]
    assert retomada.bot.position.armed_at == armed_at
    manager2.stop_all("fin")
    await asyncio.wait_for(manager2.wait_all(), 5)
    await manager2.aclose()


def test_migra_una_base_de_datos_del_esquema_1(tmp_path):
    import sqlite3
    path = str(tmp_path / "v1.db")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, live INTEGER NOT NULL,
            created_at REAL NOT NULL, status TEXT NOT NULL, settings_json TEXT NOT NULL, token_name TEXT,
            token_symbol TEXT, entry_price REAL, market_entry_price REAL, sol_amount REAL, token_amount REAL,
            entry_is_real_fill INTEGER NOT NULL DEFAULT 0, opened_at REAL, onchain_source TEXT,
            armed INTEGER NOT NULL DEFAULT 0, highest_price REAL, closed_at REAL, exit_price REAL,
            proceeds_sol REAL, realized_pnl_sol REAL, realized_pnl_pct REAL, close_reason TEXT,
            exit_is_real_fill INTEGER NOT NULL DEFAULT 0, end_reason TEXT, updated_at REAL NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO positions (mint, live, created_at, status, settings_json, updated_at)
            VALUES ('So11111111111111111111111111111111111111112', 0, 1, 'open', '{}', 1);
        PRAGMA user_version = 1;
    """)
    db.close()
    s = PositionStore(path)
    assert s.get(1)["armed_at"] is None  # columna nueva, sin perder la fila
    s.update(1, {"armed_at": 5.0})
    s.add_price(1, 1.0, 2.0)
    assert s.prices(1) == [(1.0, 2.0)]
    s.close()


def _cerrada(s, mint, live, pnl_sol, pnl_pct, sol=0.1, status="closed"):
    pos_id = s.create(mint, live=live, settings={})
    s.update(pos_id, {"status": status, "sol_amount": sol, "realized_pnl_sol": pnl_sol,
                      "realized_pnl_pct": pnl_pct, "closed_at": 1.0})
    s.add_price(pos_id, 1.0, 1.0)
    return pos_id


def test_resumen_de_pnl_separa_reales_y_simuladas(store):
    _cerrada(store, A, True, 0.05, 50.0)
    _cerrada(store, A, True, -0.02, -20.0)
    _cerrada(store, B, False, 0.01, 10.0)
    _cerrada(store, B, False, None, None, status="cancelled")  # no cuenta
    resumen = store.history_summary()
    real = resumen["real"]
    assert (real["closed"], real["wins"], real["losses"]) == (2, 1, 1)
    assert real["pnl_sol"] == pytest.approx(0.03)
    assert real["invested_sol"] == pytest.approx(0.2)
    assert (real["best_pct"], real["worst_pct"], real["avg_pct"]) == (50.0, -20.0, 15.0)
    assert resumen["sim"]["closed"] == 1 and resumen["sim"]["pnl_sol"] == pytest.approx(0.01)


def test_limpiar_historial_por_modo_y_sin_tocar_revisar_ni_activas(store):
    real = _cerrada(store, A, True, 0.05, 50.0)
    sim = _cerrada(store, B, False, 0.01, 10.0)
    cancelada = _cerrada(store, B, False, None, None, status="cancelled")
    revisar = _cerrada(store, A, True, None, None, status="review")
    activa = store.create(A, live=False, settings={})  # waiting_entry

    assert sorted(store.clear_history(live=False)) == [sim, cancelada]
    assert store.prices(sim) == []
    assert store.get(real) is not None
    assert store.clear_history(exclude_ids=(real,)) == []
    assert store.clear_history() == [real]
    assert store.get(revisar) is not None and store.get(activa) is not None
    assert store.history_summary() == {"real": None, "sim": None}


async def test_manager_limpia_historial_y_sesion(store):
    manager, conns, _ = make_manager(store)
    managed = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await _hasta(lambda: managed.bot.position is not None)
    manager.close(managed.id, "test")
    await asyncio.wait_for(managed.task, 5)
    assert [h["id"] for h in manager.history()] == [managed.id]

    assert manager.clear_history() == 1
    assert manager.history() == []
    assert manager.get(managed.id) is None  # ya no cuenta en el PnL de la sesión
    await manager.aclose()
