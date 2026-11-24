"""SQLite state store (WAL mode) shared by the orchestrator, API and CLI."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS genomes (
  id TEXT PRIMARY KEY, genome TEXT NOT NULL, description TEXT, created REAL,
  generation INTEGER, run_id TEXT,
  is_fitness REAL, is_metrics TEXT,
  oos_metrics TEXT, dsr REAL,
  ft_metrics TEXT,
  status TEXT DEFAULT 'screened'   -- screened|oos_pass|confirmed|incubating|live|retired|rejected
);
CREATE INDEX IF NOT EXISTS idx_genomes_status ON genomes(status);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started REAL, finished REAL, n_trials INTEGER, config TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS bots (
  name TEXT PRIMARY KEY, engine TEXT, genome_id TEXT, port INTEGER, status TEXT,
  config_path TEXT, pid INTEGER, started REAL, restarts INTEGER DEFAULT 0, meta TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, source TEXT, message TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL, total REAL, open_trades INTEGER, realized REAL, unrealized REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS agent_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, provider TEXT,
  proposal TEXT, applied INTEGER, reason TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def execute(self, sql: str, args: tuple = ()) -> None:
        with self._lock, self._conn() as c:
            c.execute(sql, args)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock, self._conn() as c:
            c.executemany(sql, rows)

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # ---- helpers -------------------------------------------------------------
    def event(self, level: str, source: str, message: str, **data) -> None:
        self.execute("INSERT INTO events(ts,level,source,message,data) VALUES(?,?,?,?,?)",
                     (time.time(), level, source, message, json.dumps(data, default=str)))

    def kv_get(self, k: str, default=None):
        r = self.query("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k: str, v) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (k, json.dumps(v, default=str)))

    def set_genome_status(self, gid: str, status: str) -> None:
        self.execute("UPDATE genomes SET status=? WHERE id=?", (status, gid))

    def genome(self, gid: str) -> dict | None:
        r = self.query("SELECT * FROM genomes WHERE id=?", (gid,))
        return _decode(r[0]) if r else None

    def leaderboard(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            rows = self.query("SELECT * FROM genomes WHERE status=? ORDER BY dsr DESC, is_fitness DESC "
                              "LIMIT ?", (status, limit))
        else:
            rows = self.query("SELECT * FROM genomes WHERE status!='screened' ORDER BY "
                              "COALESCE(dsr,-1) DESC, is_fitness DESC LIMIT ?", (limit,))
        return [_decode(r) for r in rows]


def _decode(r: dict) -> dict:
    for k in ("genome", "is_metrics", "oos_metrics", "ft_metrics"):
        if r.get(k):
            r[k] = json.loads(r[k])
    return r
