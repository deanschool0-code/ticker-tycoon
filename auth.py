"""Player accounts: sign up, log in, sessions. Every player gets their own portfolio, XP, lessons and watchlist.

Each player's game data is stored under the owner key 'u<user id>' (the `mode` column in the game tables).
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

import db

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
PW_METHOD = "pbkdf2:sha256:600000"   # works on every Python build (scrypt needs OpenSSL 1.1+)
_fail: Dict[str, List[float]] = {}
_fail_lock = threading.Lock()
MAX_FAILS = 8          # failed logins per (IP, username) ...
FAIL_WINDOW = 10 * 60  # ... per 10 minutes


class AuthError(Exception):
    pass


def owner_of(uid: int) -> str:
    return "u%d" % uid


def secret_key() -> str:
    k = db.kv_get("secret_key")
    if not k:
        k = secrets.token_hex(32)
        db.kv_set("secret_key", k)
    return k


def _throttled(ip: str) -> bool:
    now = time.time()
    with _fail_lock:
        lst = [t for t in _fail.get(ip, []) if now - t < FAIL_WINDOW]
        _fail[ip] = lst
        return len(lst) >= MAX_FAILS


def _record_fail(ip: str) -> None:
    with _fail_lock:
        _fail.setdefault(ip, []).append(time.time())


def signup(username: str, password: str, ip: str = "") -> dict:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 3-20 characters: letters, numbers, _ . or -")
    if len(password or "") < 6:
        raise AuthError("Password must be at least 6 characters")
    if db.q1("SELECT 1 FROM users WHERE username=?", (username,)):
        raise AuthError("That username is taken")
    first = (db.q1("SELECT COUNT(*) n FROM users") or {"n": 0})["n"] == 0
    uid = db.x("INSERT INTO users(username,pw,created_ts,last_login,is_admin) VALUES(?,?,?,?,?)",
               (username, generate_password_hash(password, method=PW_METHOD), time.time(), time.time(), 1 if first else 0))
    if first:
        _claim_single_player_save(owner_of(uid))
    return user(uid)


def _claim_single_player_save(owner: str) -> None:
    """The very first account inherits the progress from the old single-player version (if any)."""
    if not db.q1("SELECT 1 FROM accounts WHERE mode='live'"):
        return
    with db.tx():
        c = db.conn()
        for t in ("accounts", "positions", "orders", "trades", "events", "networth", "bots", "funds", "codes"):
            c.execute("UPDATE %s SET mode=? WHERE mode='live'" % t, (owner,))
        for t in ("watchlist", "lessons", "achievements"):
            c.execute("UPDATE %s SET owner=? WHERE owner='live'" % t, (owner,))
    for k in ("xp", "xp:live"):
        v = db.kv_get(k)
        if v:
            db.kv_set("xp:" + owner, int(db.kv_get("xp:" + owner, 0)) + int(v))
            db.kv_set(k, 0)


def login(username: str, password: str, ip: str = "") -> dict:
    # throttle per (IP, username): a whole school shares one IP, so never block the IP itself
    ip = "%s|%s" % (ip, (username or "").strip().lower())
    if _throttled(ip):
        raise AuthError("Too many failed attempts. Wait a few minutes and try again.")
    r = db.q1("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    if not r or not check_password_hash(r["pw"], password or ""):
        _record_fail(ip)
        raise AuthError("Wrong username or password")
    db.x("UPDATE users SET last_login=? WHERE id=?", (time.time(), r["id"]))
    return user(r["id"])


def change_password(uid: int, old: str, new: str) -> None:
    r = db.q1("SELECT * FROM users WHERE id=?", (uid,))
    if not r or not check_password_hash(r["pw"], old or ""):
        raise AuthError("Current password is wrong")
    if len(new or "") < 6:
        raise AuthError("New password must be at least 6 characters")
    db.x("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(new, method=PW_METHOD), uid))


def user(uid: Optional[int]) -> Optional[dict]:
    if not uid:
        return None
    r = db.q1("SELECT id, username, created_ts, is_admin FROM users WHERE id=?", (uid,))
    if not r:
        return None
    r["owner"] = owner_of(r["id"])
    r["is_admin"] = bool(r["is_admin"])
    return r


def leaderboard(me: Optional[int] = None, limit: int = 50) -> dict:
    rows = db.q("""SELECT u.id, u.username,
                     (SELECT value FROM networth n WHERE n.mode = 'u' || u.id ORDER BY ts DESC LIMIT 1) AS networth
                   FROM users u""")
    for r in rows:
        r["networth"] = r["networth"] if r["networth"] is not None else 10000.0
        r["xp"] = int(db.kv_get("xp:u%d" % r["id"], 0))
    rows.sort(key=lambda r: -r["networth"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["me"] = r["id"] == me
    mine = next((r for r in rows if r["me"]), None)
    top = rows[:limit]
    return {"top": [{k: r[k] for k in ("rank", "username", "networth", "xp", "me")} for r in top],
            "me": {k: mine[k] for k in ("rank", "username", "networth", "xp")} if mine else None,
            "players": len(rows)}
