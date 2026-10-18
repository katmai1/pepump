"""
Persistencia (store.py) y recuperación al arrancar (PositionManager.recover):
parar sin vender, retomar donde estaba, y los casos raros.
"""
import asyncio

import pytest

from pepump.feed import SharedTradeFeed
from pepump.manager import PositionManager
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
