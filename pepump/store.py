"""
Estado persistente de las posiciones (SQLite), para poder retomarlas al
volver a arrancar el bot.

Cada posición es una fila de `positions`. El gestor la crea al abrirla y
el bot la va reescribiendo ENTERA en cada momento importante (ver
TrailingTakeProfitBot._persist): al empezar a comprar, al comprar, al
armarse el trailing, con cada nuevo máximo, al cerrar y al terminar. No
con cada precio: el máximo y el armado son lo único del monitoreo que
hace falta para retomar la estrategia donde estaba.

`status` dice qué hacer con ella al arrancar (ver PositionManager.recover):
    waiting_entry  esperando entrada -> no se retoma (no había dinero)
    buying         compra enviada, sin confirmar -> mirar la wallet
    open           posición abierta -> se retoma
    closed         vendida
    cancelled      terminó sin comprar
    review         algo raro (p. ej. compra a medias): revisar a mano

Las escrituras son síncronas y pequeñas (una fila por evento): no merece
la pena sacarlas del loop de asyncio.
"""
import json
import logging
import sqlite3
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Columnas que el bot reescribe (todas menos id/mint/live/created_at).
POSITION_FIELDS = (
    "status", "settings_json", "token_name", "token_symbol",
    "entry_price", "market_entry_price", "sol_amount", "token_amount", "entry_is_real_fill",
    "opened_at", "onchain_source", "armed", "highest_price",
    "closed_at", "exit_price", "proceeds_sol", "realized_pnl_sol", "realized_pnl_pct",
    "close_reason", "exit_is_real_fill", "end_reason", "updated_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mint                TEXT NOT NULL,
    live                INTEGER NOT NULL,
    created_at          REAL NOT NULL,
    status              TEXT NOT NULL,
    settings_json       TEXT NOT NULL,
    token_name          TEXT,
    token_symbol        TEXT,
    entry_price         REAL,
    market_entry_price  REAL,
    sol_amount          REAL,
    token_amount        REAL,
    entry_is_real_fill  INTEGER NOT NULL DEFAULT 0,
    opened_at           REAL,
    onchain_source      TEXT,
    armed               INTEGER NOT NULL DEFAULT 0,
    highest_price       REAL,
    closed_at           REAL,
    exit_price          REAL,
    proceeds_sol        REAL,
    realized_pnl_sol    REAL,
    realized_pnl_pct    REAL,
    close_reason        TEXT,
    exit_is_real_fill   INTEGER NOT NULL DEFAULT 0,
    end_reason          TEXT,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS positions_status ON positions(status);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class PositionStore:
    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False: el loop es un solo hilo, pero los tests
        # (TestClient) llaman desde otro. Nunca hay escrituras concurrentes.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        # WAL + synchronous=NORMAL: cada escritura es rápida y un corte de
        # luz como mucho pierde la última, nunca corrompe el archivo.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} es de una versión más nueva del bot (esquema {version}).")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    # --- posiciones -------------------------------------------------------- #
    def create(self, mint: str, live: bool, settings: dict) -> int:
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO positions (mint, live, created_at, status, settings_json, updated_at) "
            "VALUES (?, ?, ?, 'waiting_entry', ?, ?)",
            (mint, int(live), now, json.dumps(settings), now))
        return cur.lastrowid

    def update(self, pos_id: int, fields: dict) -> None:
        desconocidos = set(fields) - set(POSITION_FIELDS)
        if desconocidos:
            raise ValueError(f"Campos desconocidos: {sorted(desconocidos)}")
        fields = {**fields, "updated_at": time.time()}
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(f"UPDATE positions SET {cols} WHERE id = ?", (*fields.values(), pos_id))

    def get(self, pos_id: int) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM positions WHERE id = ?", (pos_id,)).fetchone()
        return _row(row)

    def with_status(self, *statuses: str) -> List[dict]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._db.execute(f"SELECT * FROM positions WHERE status IN ({marks}) ORDER BY id",
                                statuses).fetchall()
        return [_row(r) for r in rows]

    def history(self, limit: int = 100, exclude_ids=()) -> List[dict]:
        """Posiciones terminadas, las más recientes primero."""
        rows = self._db.execute(
            "SELECT * FROM positions WHERE status IN ('closed', 'cancelled', 'review') "
            "ORDER BY COALESCE(closed_at, updated_at) DESC LIMIT ?", (limit + len(exclude_ids),)).fetchall()
        out = [_row(r) for r in rows if r["id"] not in set(exclude_ids)]
        return out[:limit]

    # --- meta -------------------------------------------------------------- #
    def get_meta(self, key: str) -> Optional[str]:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _row(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    d["live"] = bool(d["live"])
    d["armed"] = bool(d["armed"])
    d["entry_is_real_fill"] = bool(d["entry_is_real_fill"])
    d["exit_is_real_fill"] = bool(d["exit_is_real_fill"])
    d["settings"] = json.loads(d.pop("settings_json"))
    return d
