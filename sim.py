"""TURBO mode price simulator.

Each ticker starts from its REAL current price and REAL historical volatility (from daily history),
then moves with geometric Brownian motion plus rare 'news shock' jumps on an accelerated clock.
Candles are generated lazily (only for tickers you look at, hold, or run bots on) and stored in SQLite.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, List, Optional

import numpy as np

import config
import db
import fetchers

YEAR = 365 * 86400
MAX_STEPS = 3000          # max candles generated per advance call (big gaps use coarser bars)
KEEP_MINUTE_BARS = 4000
OFFLINE_CAP = 8 * 3600    # turbo offline progress is capped at 8 real hours (idle-game style)
_lock = threading.RLock()
_rng = np.random.default_rng()
PENDING: Dict[str, List[dict]] = {}   # candles generated but not yet processed by the engine (stops, bots)


def drain(aid: str) -> List[dict]:
    with _lock:
        return PENDING.pop(aid, [])


# --------------------------------------------------------------------------- clock
def speed() -> int:
    s = db.kv_get("turbo_speed", config.DEFAULT_TURBO_SPEED)
    return int(s) if s in config.TURBO_SPEEDS else config.DEFAULT_TURBO_SPEED


def _anchor() -> dict:
    a = db.kv_get("turbo_anchor")
    if not a:
        now = time.time()
        a = {"real": now, "sim": float(int(now // 60) * 60), "speed": speed()}
        db.kv_set("turbo_anchor", a)
    return a


def sim_now(real: Optional[float] = None) -> float:
    a = _anchor()
    real = real if real is not None else time.time()
    return a["sim"] + (real - a["real"]) * a["speed"]


def set_speed(new: int) -> None:
    now = time.time()
    s = sim_now(now)
    db.kv_set("turbo_speed", int(new))
    db.kv_set("turbo_anchor", {"real": now, "sim": s, "speed": int(new)})


def cap_offline(last_real: float, now: float) -> float:
    """If the app was closed for longer than OFFLINE_CAP, skip the extra time. Returns skipped seconds."""
    gap = now - last_real
    if gap <= OFFLINE_CAP:
        return 0.0
    skip = gap - OFFLINE_CAP
    a = _anchor()
    a["real"] += skip
    db.kv_set("turbo_anchor", a)
    return skip


# --------------------------------------------------------------------------- state
def state(aid: str) -> Optional[dict]:
    return db.q1("SELECT * FROM turbo_state WHERE asset_id=?", (aid,))


def ensure(aid: str) -> Optional[dict]:
    """Initialise a ticker for turbo from its real price + real volatility. None if no price is available."""
    with _lock:
        st = state(aid)
        if st:
            return st
        q = fetchers.get_quotes([aid]).get(aid)
        if not q or not q.get("price"):
            return None
        stats = fetchers.get_stats(aid)
        now_sim = float(int(sim_now() // 60) * 60)
        price = float(q["price"])
        db.x("INSERT OR REPLACE INTO turbo_state(asset_id,price,sim_ts,sigma,avg_vol,div_yield,init_ts,real_price) "
             "VALUES(?,?,?,?,?,?,?,?)", (aid, price, now_sim, stats["sigma"], stats["avg_vol"], stats["div_yield"],
                                        now_sim, price))
        _backfill(aid, price, now_sim, stats["sigma"], stats["avg_vol"])
        return state(aid)


def _backfill(aid: str, price: float, end_ts: float, sigma: float, avg_vol: float, n: int = 600) -> None:
    """Seed 600 one-minute bars that END exactly at the real current price (random-walk bridge)."""
    candles = _gen(price, sigma, avg_vol, n, 60.0, end_ts - n * 60, jumps=False)
    shift = price / candles[-1]["close"]
    for c in candles:
        for k in ("open", "high", "low", "close"):
            c[k] *= shift
    _store(aid, candles, 60)


def _gen(p0: float, sigma: float, avg_vol: float, n: int, dt: float, t0: float, jumps: bool = True) -> List[dict]:
    sub = 4
    sdt = dt / sub
    s = sigma / math.sqrt(YEAR)
    mu = 0.04 / YEAR
    z = _rng.standard_normal((n, sub))
    inc = (mu - 0.5 * s * s) * sdt + s * math.sqrt(sdt) * z
    if jumps:  # ~1 news shock every 2 simulated days
        pj = min(0.5, dt / (2 * 86400))
        hit = _rng.random(n) < pj
        if hit.any():
            daily = sigma / math.sqrt(365)
            inc[hit, 0] += _rng.normal(0, 2.5 * daily, hit.sum())
    path = p0 * np.exp(np.cumsum(inc.reshape(-1)))
    path = path.reshape(n, sub)
    out = []
    prev = p0
    vol_scale = avg_vol * dt / 86400.0
    vnoise = _rng.lognormal(0, 0.45, n)
    for i in range(n):
        seg = path[i]
        o = prev
        c = float(seg[-1])
        h = float(max(o, seg.max()))
        l = float(min(o, seg.min()))
        move = abs(c / o - 1)
        out.append({"time": int(t0 + i * dt), "end": int(t0 + (i + 1) * dt), "open": float(o), "high": h, "low": l,
                    "close": c, "volume": float(vol_scale * vnoise[i] * (1 + 40 * move))})
        prev = c
    return out


def _store(aid: str, candles: List[dict], res: int) -> None:
    if not candles:
        return
    rows = []
    if res == 60:
        rows += [(aid, 60, c["time"], c["open"], c["high"], c["low"], c["close"], c["volume"]) for c in candles]
    # aggregate into hourly bars (merge with existing first bucket)
    hourly: Dict[int, list] = {}
    for c in candles:
        b = c["time"] - c["time"] % 3600
        h = hourly.get(b)
        if h is None:
            ex = db.q1("SELECT o,h,l,c,v FROM turbo_candles WHERE asset_id=? AND res=3600 AND t=?", (aid, b)) \
                if not hourly else None
            if ex:
                h = [ex["o"], max(ex["h"], c["high"]), min(ex["l"], c["low"]), c["close"], ex["v"] + c["volume"]]
            else:
                h = [c["open"], c["high"], c["low"], c["close"], c["volume"]]
            hourly[b] = h
        else:
            h[1] = max(h[1], c["high"])
            h[2] = min(h[2], c["low"])
            h[3] = c["close"]
            h[4] += c["volume"]
    rows += [(aid, 3600, b, *v) for b, v in hourly.items()]
    db.xmany("INSERT OR REPLACE INTO turbo_candles(asset_id,res,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?,?)", rows)


def advance(aid: str, to_ts: Optional[float] = None) -> List[dict]:
    """Generate candles from the ticker's last simulated time up to to_ts. Returns the new candles."""
    with _lock:
        st = ensure(aid)
        if not st:
            return []
        to_ts = to_ts if to_ts is not None else sim_now()
        gap = to_ts - st["sim_ts"]
        n = int(gap // 60)
        if n < 1:
            return []
        dt = 60.0
        if n > MAX_STEPS:
            dt = gap / MAX_STEPS
            n = MAX_STEPS
        candles = _gen(st["price"], st["sigma"], st["avg_vol"], n, dt, st["sim_ts"])
        new_ts = st["sim_ts"] + n * dt
        if dt == 60.0:
            _store(aid, candles, 60)
        else:
            _store(aid, candles, 3600)
            # keep the 1m chart continuous: add a minute-bar at the end
            last = candles[-1]
            _store(aid, [dict(last, time=int(new_ts - new_ts % 60) - 60)], 60)
        db.x("UPDATE turbo_state SET price=?, sim_ts=? WHERE asset_id=?", (candles[-1]["close"], new_ts, aid))
        buf = PENDING.setdefault(aid, [])
        buf.extend(candles)
        if len(buf) > 6000:
            del buf[:-6000]
        cnt = db.q1("SELECT COUNT(*) n FROM turbo_candles WHERE asset_id=? AND res=60", (aid,))["n"]
        if cnt > KEEP_MINUTE_BARS + 500:
            db.x("DELETE FROM turbo_candles WHERE asset_id=? AND res=60 AND t <= (SELECT t FROM turbo_candles "
                 "WHERE asset_id=? AND res=60 ORDER BY t DESC LIMIT 1 OFFSET ?)", (aid, aid, KEEP_MINUTE_BARS))
        return candles


def price(aid: str) -> Optional[float]:
    advance(aid)
    st = state(aid)
    return st["price"] if st else None


def quote(aid: str) -> Optional[dict]:
    p = price(aid)
    if p is None:
        return None
    st = state(aid)
    day_ago = st["sim_ts"] - 86400
    r = db.q1("SELECT c FROM turbo_candles WHERE asset_id=? AND res=60 AND t<=? ORDER BY t DESC LIMIT 1", (aid, day_ago))
    if not r:
        r = db.q1("SELECT o AS c FROM turbo_candles WHERE asset_id=? AND res=60 ORDER BY t ASC LIMIT 1", (aid,))
    prev = r["c"] if r else st["real_price"]
    return {"price": p, "prev_close": prev, "change_pct": (p / prev - 1) * 100 if prev else 0.0,
            "ts": time.time(), "stale": False, "source": "turbo", "sim_ts": st["sim_ts"]}


def _agg(rows: List[dict], step: int) -> List[dict]:
    out: Dict[int, dict] = {}
    for x in rows:
        b = x["time"] - x["time"] % step
        k = out.get(b)
        if not k:
            out[b] = dict(x, time=b)
        else:
            k["high"] = max(k["high"], x["high"])
            k["low"] = min(k["low"], x["low"])
            k["close"] = x["close"]
            k["volume"] += x["volume"]
    return [out[k] for k in sorted(out)]


def candles(aid: str, tf: str) -> List[dict]:
    advance(aid)
    st = state(aid)
    if not st:
        return []

    def load(res):
        return [{"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"], "volume": r["v"]}
                for r in db.q("SELECT * FROM turbo_candles WHERE asset_id=? AND res=? ORDER BY t", (aid, res))]

    if tf == "1m":
        return load(60)[-1500:]
    if tf == "5m":
        return _agg(load(60), 300)[-1500:]
    step = {"1h": 3600, "1D": 86400, "1W": 604800}.get(tf, 3600)
    simbars = load(3600)
    real = [c for c in fetchers.get_candles(aid, tf) if c["time"] + step <= st["init_ts"]]
    sim = _agg(simbars, step) if step > 3600 else simbars
    sim = [c for c in sim if c["time"] >= (real[-1]["time"] + step if real else 0)]
    return (real + sim)[-1500:]


def dividend_yield(aid: str) -> float:
    st = state(aid)
    return float(st["div_yield"] or 0) if st else 0.0


def reset() -> None:
    db.x("DELETE FROM turbo_state")
    db.x("DELETE FROM turbo_candles")
    db.kv_set("turbo_anchor", None)
