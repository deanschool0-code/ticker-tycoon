"""SQLite storage (game.db). One shared connection guarded by a re-entrant lock."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional

import config

LOCK = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY, kind TEXT, symbol TEXT, name TEXT, exchange TEXT,
    rank INTEGER, logo TEXT, sector TEXT, yahoo TEXT);
CREATE INDEX IF NOT EXISTS ix_symbols_kind ON symbols(kind);
CREATE TABLE IF NOT EXISTS last_price (id TEXT PRIMARY KEY, price REAL, prev_close REAL, change_pct REAL, ts REAL);
CREATE TABLE IF NOT EXISTS accounts (
    mode TEXT PRIMARY KEY, cash REAL, created_ts REAL, starter_given INTEGER DEFAULT 0,
    last_settle_ts REAL, last_seen_ts REAL, realized REAL DEFAULT 0, dividends REAL DEFAULT 0,
    sim_ts REAL, peak_networth REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, asset_id TEXT, side TEXT, qty REAL, entry REAL,
    leverage REAL, margin REAL, sl REAL, tp REAL, opened_ts REAL, bot_id INTEGER, fund_id INTEGER,
    last_div_ts REAL, warned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, asset_id TEXT, kind TEXT, side TEXT,
    amount_usd REAL, qty REAL, leverage REAL, sl REAL, tp REAL, position_id INTEGER, fraction REAL,
    status TEXT, created_ts REAL, filled_ts REAL, fill_price REAL, note TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, asset_id TEXT, side TEXT, qty REAL, entry REAL,
    exit REAL, pnl REAL, pnl_pct REAL, reason TEXT, leverage REAL, opened_ts REAL, closed_ts REAL,
    xp INTEGER DEFAULT 0, bot_id INTEGER, fund_id INTEGER);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, ts REAL, type TEXT, asset_id TEXT,
    amount REAL, text TEXT);
CREATE INDEX IF NOT EXISTS ix_events ON events(mode, ts);
CREATE TABLE IF NOT EXISTS networth (mode TEXT, ts REAL, value REAL);
CREATE INDEX IF NOT EXISTS ix_networth ON networth(mode, ts);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE COLLATE NOCASE,
    pw TEXT, created_ts REAL, last_login REAL, is_admin INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS watchlist (owner TEXT, asset_id TEXT, added_ts REAL, PRIMARY KEY(owner, asset_id));
CREATE TABLE IF NOT EXISTS codes (code TEXT, mode TEXT, ts REAL, PRIMARY KEY(code, mode));
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, kind TEXT, name TEXT, tickers TEXT,
    budget REAL, cash REAL, params TEXT, state TEXT, active INTEGER DEFAULT 1, created_ts REAL,
    last_run_ts REAL, trades INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS funds (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, name TEXT, holdings TEXT, invested REAL DEFAULT 0,
    created_ts REAL);
CREATE TABLE IF NOT EXISTS lessons (owner TEXT, id TEXT, completed INTEGER DEFAULT 0, quiz_passed INTEGER DEFAULT 0, ts REAL,
    PRIMARY KEY(owner, id));
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, value TEXT);
CREATE TABLE IF NOT EXISTS achievements (owner TEXT, id TEXT, ts REAL, PRIMARY KEY(owner, id));
CREATE TABLE IF NOT EXISTS turbo_state (asset_id TEXT PRIMARY KEY, price REAL, sim_ts REAL, sigma REAL,
    avg_vol REAL, div_yield REAL DEFAULT 0, init_ts REAL, real_price REAL);
CREATE TABLE IF NOT EXISTS turbo_candles (asset_id TEXT, res INTEGER, t INTEGER, o REAL, h REAL, l REAL,
    c REAL, v REAL, PRIMARY KEY(asset_id, res, t));
"""


def conn() -> sqlite3.Connection:
    global _conn
    with LOCK:
        if _conn is None:
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _migrate(_conn)
            _conn.executescript(SCHEMA)
        return _conn


def _cols(c: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()]


def _migrate(c: sqlite3.Connection) -> None:
    """Older single-player saves: give per-player tables an owner column (old data belongs to 'live')."""
    for table, cols in (("watchlist", "asset_id, added_ts"), ("lessons", "id, completed, quiz_passed, ts"),
                        ("achievements", "id, ts")):
        existing = _cols(c, table)
        if existing and "owner" not in existing:
            c.execute("ALTER TABLE %s RENAME TO %s_old" % (table, table))
            c.executescript(SCHEMA)
            c.execute("INSERT INTO %s(owner, %s) SELECT 'live', %s FROM %s_old" % (table, cols, cols, table))
            c.execute("DROP TABLE %s_old" % table)


def reset_connection() -> None:
    global _conn
    with LOCK:
        if _conn is not None:
            _conn.close()
        _conn = None


def q(sql: str, args: tuple = ()) -> list:
    with LOCK:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> Optional[dict]:
    with LOCK:
        r = conn().execute(sql, args).fetchone()
        return dict(r) if r else None


def x(sql: str, args: tuple = ()) -> int:
    with LOCK:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def xmany(sql: str, rows: list) -> None:
    with LOCK:
        c = conn()
        c.execute("BEGIN")
        try:
            c.executemany(sql, rows)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


class tx:
    """with db.tx(): ... -> atomic block (re-entrant safe)."""

    depth = 0

    def __enter__(self):
        LOCK.acquire()
        if tx.depth == 0:
            conn().execute("BEGIN")
        tx.depth += 1
        return self

    def __exit__(self, et, ev, tb):
        tx.depth -= 1
        try:
            if tx.depth == 0:
                conn().execute("ROLLBACK" if et else "COMMIT")
        finally:
            LOCK.release()
        return False


def kv_get(key: str, default: Any = None) -> Any:
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return default


def kv_set(key: str, value: Any) -> None:
    x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value)))


def cache_get(key: str, ttl: float, now: float) -> Any:
    r = q1("SELECT ts, value FROM cache WHERE key=?", (key,))
    if r and now - r["ts"] < ttl:
        try:
            return json.loads(r["value"])
        except Exception:
            return None
    return None


def cache_get_stale(key: str) -> Any:
    r = q1("SELECT value FROM cache WHERE key=?", (key,))
    return json.loads(r["value"]) if r else None


def cache_set(key: str, value: Any, now: float) -> None:
    x("INSERT INTO cache(key,ts,value) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, value=excluded.value",
      (key, now, json.dumps(value)))
