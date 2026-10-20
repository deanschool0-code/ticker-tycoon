"""Game engine: accounts, orders, positions, stops, leverage/margin, XP & unlocks, bots, funds,
dividends, achievements and settlement (including offline 'while you were away' catch-up)."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Dict, List, Optional

import config
import db
import fetchers
import markethours
import sim

log = logging.getLogger("tt.engine")
MODES = ("live", "turbo")
_OWNER_RE = __import__("re").compile(r"^u\d+$")


def valid_mode(mode: str) -> bool:
    """A 'mode' is the owner of an account: 'u<user id>' for players ('live'/'turbo' kept for old saves/tests)."""
    return mode in MODES or bool(_OWNER_RE.match(mode or ""))


def is_live(mode: str) -> bool:
    return mode != "turbo"


class GameError(Exception):
    pass


# =========================================================================== helpers
def now() -> float:
    return time.time()


def is_stock(aid: str) -> bool:
    return aid.startswith("S:")


def get_setting(owner: str, key: str, default=None):
    v = db.kv_get("set:%s:%s" % (owner, key))
    return default if v is None else v


def set_setting(owner: str, key: str, value) -> None:
    db.kv_set("set:%s:%s" % (owner, key), value)


def follows_hours(mode: str) -> bool:
    """Players can switch market hours off in Settings: then stocks trade any time at the latest real price."""
    return bool(get_setting(mode, "market_hours", True))


def market_open(mode: str, aid: str, ts: Optional[float] = None) -> bool:
    if mode == "turbo" or not is_stock(aid) or not follows_hours(mode):
        return True
    return markethours.is_open(ts if ts is not None else now())


def log_event(mode: str, typ: str, text: str, aid: Optional[str] = None, amount: float = 0.0,
              ts: Optional[float] = None) -> None:
    db.x("INSERT INTO events(mode,ts,type,asset_id,amount,text) VALUES(?,?,?,?,?,?)",
         (mode, ts if ts is not None else now(), typ, aid, amount, text))


def sym(aid: str) -> str:
    a = fetchers.asset(aid)
    if not a:
        return aid[2:]
    return a["symbol"].upper()


def money(v: float) -> str:
    s = "-" if v < 0 else ""
    return "%s$%s" % (s, "{:,.2f}".format(abs(v)))


# =========================================================================== XP & levels
def xp_for_level(level: int) -> int:
    return 50 * level * (level - 1)


def level_from_xp(xp: int) -> int:
    lv = 1
    while xp >= xp_for_level(lv + 1):
        lv += 1
    return lv


def profile(mode: str = "live") -> dict:
    xp = int(db.kv_get("xp:" + mode, 0))
    lv = level_from_xp(xp)
    return {"xp": xp, "level": lv, "level_xp": xp_for_level(lv), "next_xp": xp_for_level(lv + 1),
            "unlocks": [], "max_leverage": 10, "rank": rank(lv),
            "next_rank": next((r for l, r in config.RANKS if l > lv), None),
            "next_rank_level": next((l for l, r in config.RANKS if l > lv), None)}


def rank(lv: int) -> str:
    name = config.RANKS[0][1]
    for l, r in config.RANKS:
        if lv >= l:
            name = r
    return name


def unlocked(key: str, lv: Optional[int] = None) -> bool:
    return True  # nothing is locked


def max_leverage(lv: Optional[int] = None) -> int:
    return 10


def add_xp(n: int, reason: str, mode: str = "live") -> dict:
    if n <= 0:
        return {"xp": 0}
    before = level_from_xp(int(db.kv_get("xp:" + mode, 0)))
    total = int(db.kv_get("xp:" + mode, 0)) + int(n)
    db.kv_set("xp:" + mode, total)
    after = level_from_xp(total)
    res = {"xp": n, "reason": reason, "level_up": after > before, "level": after, "unlocked": []}
    if after > before:
        for u in config.UNLOCKS:
            if before < u["level"] <= after:
                res["unlocked"].append(u["label"])
        txt = "Level up! You reached level %d (%s)" % (after, rank(after))
        if res["unlocked"]:
            txt += " and unlocked " + ", ".join(res["unlocked"])
        log_event(mode, "level_up", txt)
        pend = db.kv_get("toasts:" + mode, []) or []
        pend.append({"type": "level", "text": txt})
        db.kv_set("toasts:" + mode, pend[-10:])
    return res


def trade_xp(pnl: float, pnl_pct: float, reason: str, notional: float) -> int:
    if notional < 100 or reason == "liquidation":
        return 0
    if pnl > 0:
        return int(20 + min(80, pnl_pct * 4))
    if reason == "stop" and pnl_pct > -6:
        return 12  # smart stop-out: you cut a loss small
    return 5


# =========================================================================== accounts
def account(mode: str) -> dict:
    if not valid_mode(mode):
        raise GameError("unknown account")
    a = db.q1("SELECT * FROM accounts WHERE mode=?", (mode,))
    if not a:
        t = now()
        db.x("INSERT INTO accounts(mode,cash,created_ts,last_settle_ts,last_seen_ts,sim_ts) VALUES(?,?,?,?,?,?)",
             (mode, config.START_CASH, t, t, t, sim.sim_now(t) if mode == "turbo" else None))
        log_event(mode, "start", "Account opened with %s virtual cash" % money(config.START_CASH))
        a = db.q1("SELECT * FROM accounts WHERE mode=?", (mode,))
    return a


def adj_cash(mode: str, delta: float) -> None:
    db.x("UPDATE accounts SET cash=cash+? WHERE mode=?", (delta, mode))


def give_starter(mode: str) -> None:
    a = account(mode)
    if a["starter_given"]:
        return
    p = price(mode, config.STARTER_SYMBOL)
    if not p:
        return
    _open(mode, config.STARTER_SYMBOL, "long", config.STARTER_POSITION_USD, 1.0, p, None, None,
          free=True, note="starter")
    db.x("UPDATE accounts SET starter_given=1 WHERE mode=?", (mode,))
    log_event(mode, "starter", "Free starter position: %s of SPY" % money(config.STARTER_POSITION_USD),
              config.STARTER_SYMBOL, config.STARTER_POSITION_USD)


# =========================================================================== prices
def quotes(mode: str, aids: List[str]) -> Dict[str, dict]:
    if mode == "turbo":
        out = {}
        for a in aids:
            q = sim.quote(a)
            if q:
                out[a] = q
        return out
    return fetchers.get_quotes(aids)


def price(mode: str, aid: str) -> Optional[float]:
    q = quotes(mode, [aid]).get(aid)
    return q["price"] if q else None


# =========================================================================== positions
def _pos(pid: int) -> dict:
    p = db.q1("SELECT * FROM positions WHERE id=?", (pid,))
    if not p:
        raise GameError("position not found")
    return p


def upnl(p: dict, px: float) -> float:
    d = 1 if p["side"] == "long" else -1
    return (px - p["entry"]) * p["qty"] * d


def _validate_stops(side: str, px: float, sl: Optional[float], tp: Optional[float]) -> None:
    if side == "long":
        if sl is not None and sl >= px:
            raise GameError("For a LONG, the stop loss must be below the current price (%s)." % money(px))
        if tp is not None and tp <= px:
            raise GameError("For a LONG, the take profit must be above the current price (%s)." % money(px))
    else:
        if sl is not None and sl <= px:
            raise GameError("For a SHORT, the stop loss must be above the current price (%s)." % money(px))
        if tp is not None and tp >= px:
            raise GameError("For a SHORT, the take profit must be below the current price (%s)." % money(px))


def _open(mode: str, aid: str, side: str, notional: float, lev: float, px: float, sl: Optional[float],
          tp: Optional[float], bot_id: Optional[int] = None, fund_id: Optional[int] = None, merge: bool = False,
          free: bool = False, ts: Optional[float] = None, note: str = "", reserved: float = 0.0) -> int:
    """Open (or add to) a position. Cash/margin must already be checked by the caller."""
    ts = ts if ts is not None else now()
    margin = notional / lev
    qty = notional / px
    if not free:
        if bot_id:
            db.x("UPDATE bots SET cash=cash-? WHERE id=?", (margin, bot_id))
        else:
            adj_cash(mode, -(margin - reserved))
    if merge:
        ex = db.q1("SELECT * FROM positions WHERE mode=? AND asset_id=? AND side=? AND IFNULL(bot_id,0)=? "
                   "AND IFNULL(fund_id,0)=? AND leverage=?", (mode, aid, side, bot_id or 0, fund_id or 0, lev))
        if ex:
            nq = ex["qty"] + qty
            ne = (ex["entry"] * ex["qty"] + px * qty) / nq
            db.x("UPDATE positions SET qty=?, entry=?, margin=margin+? WHERE id=?", (nq, ne, margin, ex["id"]))
            return ex["id"]
    pid = db.x("INSERT INTO positions(mode,asset_id,side,qty,entry,leverage,margin,sl,tp,opened_ts,bot_id,fund_id,"
               "last_div_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (mode, aid, side, qty, px, lev, margin, sl, tp, ts, bot_id, fund_id,
                sim.sim_now() if mode == "turbo" else ts))
    return pid


def _close(p: dict, px: float, fraction: float, reason: str, ts: Optional[float] = None) -> dict:
    """Close part/all of a position at px. Returns the trade record."""
    ts = ts if ts is not None else now()
    mode = p["mode"]
    fraction = max(0.0, min(1.0, fraction))
    if p["qty"] * (1 - fraction) * px < 0.01:
        fraction = 1.0
    q = p["qty"] * fraction
    margin = p["margin"] * fraction
    d = 1 if p["side"] == "long" else -1
    pnl = (px - p["entry"]) * q * d
    payout = margin + pnl
    if reason == "liquidation":
        payout = max(0.0, payout)
    if p["bot_id"]:
        db.x("UPDATE bots SET cash=cash+?, trades=trades+1 WHERE id=?", (payout, p["bot_id"]))
    else:
        adj_cash(mode, payout)
    if fraction >= 0.999999:
        db.x("DELETE FROM positions WHERE id=?", (p["id"],))
    else:
        db.x("UPDATE positions SET qty=qty-?, margin=margin-? WHERE id=?", (q, margin, p["id"]))
    pnl_pct = pnl / margin * 100 if margin else 0.0
    manual = not p["bot_id"] and not p["fund_id"]
    xp = trade_xp(pnl, pnl_pct, reason, q * p["entry"]) if manual else 0
    tid = db.x("INSERT INTO trades(mode,asset_id,side,qty,entry,exit,pnl,pnl_pct,reason,leverage,opened_ts,closed_ts,xp,"
               "bot_id,fund_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (mode, p["asset_id"], p["side"], q, p["entry"], px, pnl, pnl_pct, reason, p["leverage"],
                p["opened_ts"], ts, xp, p["bot_id"], p["fund_id"]))
    db.x("UPDATE accounts SET realized=realized+? WHERE mode=?", (pnl, mode))
    xpres = add_xp(xp, "trade", mode) if xp else {"xp": 0}
    return {"trade_id": tid, "pnl": pnl, "pnl_pct": pnl_pct, "price": px, "qty": q, "xp": xp,
            "level_up": xpres.get("level_up", False), "unlocked": xpres.get("unlocked", [])}


def _sync(mode: str) -> None:
    """Turbo: process any simulated candles first so new positions never see stale price moves."""
    if mode == "turbo":
        settle("turbo", force=True)


def place_order(mode: str, aid: str, side: str, amount_usd: Optional[float] = None, qty: Optional[float] = None,
                leverage: float = 1, sl: Optional[float] = None, tp: Optional[float] = None) -> dict:
    account(mode)
    _sync(mode)
    if side not in ("long", "short"):
        raise GameError("side must be long or short")
    a = fetchers.asset(aid)
    if not a:
        raise GameError("Unknown ticker")
    leverage = float(leverage or 1)
    if leverage not in (1, 2, 5, 10):
        raise GameError("Leverage must be 1x, 2x, 5x or 10x")
    if leverage > max_leverage():
        raise GameError("%dx leverage is locked. Level up to unlock it." % leverage)
    px = price(mode, aid)
    if not px:
        raise GameError("No price available for %s right now. Check your connection and try again." % sym(aid))
    if qty:
        amount_usd = float(qty) * px
    amount_usd = float(amount_usd or 0)
    if amount_usd < 1:
        raise GameError("Trade size must be at least $1")
    sl = float(sl) if sl not in (None, "", 0) else None
    tp = float(tp) if tp not in (None, "", 0) else None
    margin = amount_usd / leverage
    cash = account(mode)["cash"]
    if margin > cash + 1e-6:
        raise GameError("Not enough cash: this trade needs %s of margin, you have %s." % (money(margin), money(cash)))
    if not market_open(mode, aid):
        # queue for the next open; reserve the margin now
        if sl is not None or tp is not None:
            _validate_stops(side, px, sl, tp)
        adj_cash(mode, -margin)
        oid = db.x("INSERT INTO orders(mode,asset_id,kind,side,amount_usd,leverage,sl,tp,status,created_ts,note) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (mode, aid, "open", side, amount_usd, leverage, sl, tp, "queued",
                                                    now(), "queued: market closed"))
        st = markethours.status(now())
        log_event(mode, "order_queued", "Queued %s %s %s for the open (%s)" % (side.upper(), money(amount_usd), sym(aid),
                                                                               st.get("next_open_text", "")), aid, amount_usd)
        return {"queued": True, "order_id": oid, "message": "Market closed. Order queued for %s." % st.get("next_open_text", "the open")}
    _validate_stops(side, px, sl, tp)
    pid = _open(mode, aid, side, amount_usd, leverage, px, sl, tp)
    log_event(mode, "open", "Opened %s %s on %s at %s%s" % (side.upper(), money(amount_usd), sym(aid), money(px),
                                                            " (%dx)" % leverage if leverage > 1 else ""), aid, amount_usd)
    check_achievements(mode)
    return {"queued": False, "position_id": pid, "price": px, "qty": amount_usd / px}


def close_position(pid: int, fraction: float = 1.0) -> dict:
    p = _pos(pid)
    mode = p["mode"]
    if p["fund_id"]:
        raise GameError("This position belongs to a fund. Withdraw from the fund in the Funds tab.")
    if p["bot_id"]:
        raise GameError("This position is managed by a bot. Stop or sell the bot in the Bots tab.")
    if not market_open(mode, p["asset_id"]):
        oid = db.x("INSERT INTO orders(mode,asset_id,kind,side,position_id,fraction,status,created_ts,note) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (mode, p["asset_id"], "close", p["side"], pid, fraction, "queued",
                                                 now(), "close queued: market closed"))
        st = markethours.status(now())
        return {"queued": True, "order_id": oid, "message": "Market closed. Close queued for %s." % st.get("next_open_text", "the open")}
    px = price(mode, p["asset_id"])
    if not px:
        raise GameError("No price available right now")
    res = _close(p, px, fraction, "manual")
    log_event(mode, "close", "Closed %s%s %s at %s: %s" % ("" if fraction >= 1 else "%d%% of " % round(fraction * 100),
                                                         p["side"].upper(), sym(p["asset_id"]), money(px), money(res["pnl"])),
              p["asset_id"], res["pnl"])
    check_achievements(mode)
    return res


def set_stops(pid: int, sl: Optional[float], tp: Optional[float]) -> dict:
    p = _pos(pid)
    sl = float(sl) if sl not in (None, "", 0) else None
    tp = float(tp) if tp not in (None, "", 0) else None
    px = price(p["mode"], p["asset_id"]) or p["entry"]
    _validate_stops(p["side"], px, sl, tp)
    db.x("UPDATE positions SET sl=?, tp=? WHERE id=?", (sl, tp, pid))
    return {"ok": True, "sl": sl, "tp": tp}


def add_margin(pid: int, amount: float) -> dict:
    p = _pos(pid)
    amount = float(amount)
    if amount <= 0 or amount > account(p["mode"])["cash"]:
        raise GameError("Not enough cash")
    adj_cash(p["mode"], -amount)
    notional = p["qty"] * p["entry"]
    nm = p["margin"] + amount
    db.x("UPDATE positions SET margin=?, leverage=?, warned=0 WHERE id=?", (nm, max(1.0, notional / nm), pid))
    log_event(p["mode"], "margin", "Added %s margin to %s" % (money(amount), sym(p["asset_id"])), p["asset_id"], amount)
    return {"ok": True}


def cancel_order(oid: int) -> dict:
    o = db.q1("SELECT * FROM orders WHERE id=? AND status='queued'", (oid,))
    if not o:
        raise GameError("Order not found")
    if o["kind"] == "open":
        adj_cash(o["mode"], o["amount_usd"] / (o["leverage"] or 1))
    db.x("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    return {"ok": True}


# =========================================================================== codes
def redeem(mode: str, code: str) -> dict:
    code = (code or "").strip().upper()
    c = config.CODES.get(code)
    if not c:
        raise GameError("That code isn't valid.")
    scope = mode
    if db.q1("SELECT 1 FROM codes WHERE code=? AND mode=?", (code, scope)):
        raise GameError("You already redeemed %s%s." % (code, " in %s mode" % mode.upper() if scope != "*" else ""))
    account(mode)
    db.x("INSERT INTO codes(code,mode,ts) VALUES(?,?,?)", (code, scope, now()))
    if "cash" in c:
        adj_cash(mode, c["cash"])
    if "xp" in c:
        add_xp(c["xp"], "code", mode)
    log_event(mode, "code", "Redeemed code %s: %s" % (code, c["label"]), None, c.get("cash", 0))
    return {"ok": True, "label": c["label"]}


# =========================================================================== bots
def _closes_for(mode: str, aid: str, bar: int) -> List[float]:
    try:
        if mode == "turbo":
            sim.advance(aid)
            rows = db.q("SELECT t, c FROM turbo_candles WHERE asset_id=? AND res=60 ORDER BY t", (aid,))
            out: Dict[int, float] = {}
            for r in rows:
                out[r["t"] - r["t"] % bar] = r["c"]
            return [out[k] for k in sorted(out)][-120:]
        cs = fetchers.get_candles(aid, "5m")
        return [c["close"] for c in cs][-120:]
    except Exception:
        return []


def create_bot(mode: str, kind: str, tickers: List[str], budget: float, params: Optional[dict] = None) -> dict:
    account(mode)
    bt = config.BOT_TYPES.get(kind)
    if not bt:
        raise GameError("Unknown bot type")
    tickers = [t for t in dict.fromkeys(tickers or []) if fetchers.asset(t)][:5]
    if not tickers:
        raise GameError("Pick at least one ticker (max 5)")
    if bt["crypto_only"] and any(not t.startswith("C:") for t in tickers):
        raise GameError("The Grid Bot only trades crypto")
    budget = float(budget)
    if budget < 100:
        raise GameError("Bot budget must be at least $100")
    cost = bt["price"] + budget
    if cost > account(mode)["cash"]:
        raise GameError("You need %s (bot %s + budget %s)" % (money(cost), money(bt["price"]), money(budget)))
    params = dict(params or {})
    params.setdefault("bar", 300 if is_live(mode) else 900)
    if kind == "dca":
        params.setdefault("amount", round(max(10.0, budget / 10 / len(tickers)), 2))
        params.setdefault("interval", 3600 if is_live(mode) else 6 * 3600)
    if kind == "grid":
        params.setdefault("levels", 6)
        params.setdefault("spacing", 0.01)
    _sync(mode)
    adj_cash(mode, -cost)
    state = {"closes": {}, "bucket": {}, "last_close": {}, "last_buy": {}, "peak": {}, "grid": {}}
    for t in tickers:
        state["closes"][t] = _closes_for(mode, t, params["bar"])
        if kind == "grid":
            px = price(mode, t)
            if px:
                lv = params["levels"]
                state["grid"][t] = {"base": px, "lots": {str(i): None for i in range(1, lv + 1)}}
    bid = db.x("INSERT INTO bots(mode,kind,name,tickers,budget,cash,params,state,active,created_ts,last_run_ts) "
               "VALUES(?,?,?,?,?,?,?,?,1,?,?)", (mode, kind, bt["name"], json.dumps(tickers), budget, budget,
                                                 json.dumps(params), json.dumps(state), now(), now()))
    log_event(mode, "bot", "Bought %s (budget %s) for %s" % (bt["name"], money(budget), ", ".join(sym(t) for t in tickers)))
    check_achievements(mode)
    return {"ok": True, "bot_id": bid}


def toggle_bot(bid: int) -> dict:
    b = db.q1("SELECT * FROM bots WHERE id=?", (bid,))
    if not b:
        raise GameError("bot not found")
    db.x("UPDATE bots SET active=? WHERE id=?", (0 if b["active"] else 1, bid))
    return {"ok": True, "active": not b["active"]}


def sell_bot(bid: int) -> dict:
    """Close the bot's positions and return its cash (+ half the purchase price) to the account."""
    b = db.q1("SELECT * FROM bots WHERE id=?", (bid,))
    if not b:
        raise GameError("bot not found")
    mode = b["mode"]
    for p in db.q("SELECT * FROM positions WHERE bot_id=?", (bid,)):
        px = price(mode, p["asset_id"]) or p["entry"]
        _close(p, px, 1.0, "bot")
    b = db.q1("SELECT * FROM bots WHERE id=?", (bid,))
    refund = b["cash"] + config.BOT_TYPES[b["kind"]]["price"] * 0.5
    adj_cash(mode, refund)
    db.x("DELETE FROM bots WHERE id=?", (bid,))
    log_event(mode, "bot", "Sold %s, returned %s to cash" % (b["name"], money(refund)), None, refund)
    return {"ok": True, "refund": refund}


def _sma(xs: List[float], n: int) -> Optional[float]:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _rsi(xs: List[float], n: int = 14) -> Optional[float]:
    if len(xs) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(len(xs) - n, len(xs)):
        d = xs[i] - xs[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


class _BotRun:
    def __init__(self, b: dict):
        self.b = b
        self.params = json.loads(b["params"] or "{}")
        self.st = json.loads(b["state"] or "{}")
        for k in ("closes", "bucket", "last_close", "last_buy", "peak", "grid"):
            self.st.setdefault(k, {})
        self.tickers = json.loads(b["tickers"])
        self.dirty = False

    def cash(self) -> float:
        return db.q1("SELECT cash FROM bots WHERE id=?", (self.b["id"],))["cash"]

    def pos(self, aid: str) -> Optional[dict]:
        return db.q1("SELECT * FROM positions WHERE bot_id=? AND asset_id=? ORDER BY id LIMIT 1", (self.b["id"], aid))

    def buy(self, aid: str, usd: float, px: float, ts: float, merge: bool = True, why: str = "") -> Optional[int]:
        usd = min(usd, self.cash())
        if usd < 1:
            return None
        pid = _open(self.b["mode"], aid, "long", usd, 1.0, px, None, None, bot_id=self.b["id"], merge=merge, ts=ts)
        db.x("UPDATE bots SET trades=trades+1 WHERE id=?", (self.b["id"],))
        log_event(self.b["mode"], "bot_trade", "%s bought %s of %s at %s%s" % (self.b["name"], money(usd), sym(aid),
                                                                              money(px), (" (" + why + ")") if why else ""),
                  aid, -usd, ts)
        return pid

    def sell(self, p: dict, px: float, ts: float, why: str = "") -> None:
        r = _close(p, px, 1.0, "bot", ts)
        log_event(self.b["mode"], "bot_trade", "%s sold %s at %s: %s%s" % (self.b["name"], sym(p["asset_id"]), money(px),
                                                                          money(r["pnl"]), (" (" + why + ")") if why else ""),
                  p["asset_id"], r["pnl"], ts)

    def feed(self, aid: str, c: dict, ts: float) -> None:
        if aid not in self.tickers:
            return
        kind = self.b["kind"]
        px = c["close"]
        if kind == "dca":
            last = self.st["last_buy"].get(aid, 0)
            t = c.get("end", c["time"])
            if t - last >= self.params.get("interval", 3600):
                if self.buy(aid, float(self.params.get("amount", 50)), px, ts, merge=True, why="scheduled DCA"):
                    self.st["last_buy"][aid] = t
                    self.dirty = True
            return
        if kind == "grid":
            g = self.st["grid"].get(aid)
            if not g:
                g = {"base": px, "lots": {str(i): None for i in range(1, int(self.params.get("levels", 6)) + 1)}}
                self.st["grid"][aid] = g
                self.dirty = True
            sp = float(self.params.get("spacing", 0.01))
            nlev = len(g["lots"])
            lot_usd = self.b["budget"] / max(1, nlev) / max(1, len(self.tickers))
            for k, pid in list(g["lots"].items()):
                level = g["base"] * (1 - int(k) * sp)
                if pid is None and c["low"] <= level:
                    np_ = self.buy(aid, lot_usd, min(level, c["open"]), ts, merge=False, why="grid level -%d%%" % round(int(k) * sp * 100))
                    if np_:
                        g["lots"][k] = np_
                        self.dirty = True
                elif pid is not None:
                    p = db.q1("SELECT * FROM positions WHERE id=?", (pid,))
                    if not p:
                        g["lots"][k] = None
                        self.dirty = True
                        continue
                    target = level * (1 + sp)
                    if c["high"] >= target:
                        self.sell(p, max(target, c["open"]), ts, why="grid take-profit")
                        g["lots"][k] = None
                        self.dirty = True
            return
        # bar-based strategies
        bar = int(self.params.get("bar", 300))
        b = c["time"] - c["time"] % bar
        cur = self.st["bucket"].get(aid)
        closes = self.st["closes"].setdefault(aid, [])
        if cur is None:
            self.st["bucket"][aid] = b
            self.st["last_close"][aid] = px
            self.dirty = True
            return
        if b != cur:  # previous bar completed
            closes.append(self.st["last_close"].get(aid, px))
            del closes[:-150]
            self.st["bucket"][aid] = b
            self.dirty = True
            self.on_bar(aid, closes, px, ts)
        self.st["last_close"][aid] = px
        if kind == "momentum":
            p = self.pos(aid)
            if p:
                pk = max(self.st["peak"].get(aid, p["entry"]), c["high"])
                self.st["peak"][aid] = pk
                if px <= pk * 0.98:
                    self.sell(p, px, ts, why="2% trailing stop")
                    self.st["peak"].pop(aid, None)
                self.dirty = True

    def alloc(self) -> float:
        return self.cash() / max(1, sum(1 for t in self.tickers if not self.pos(t)))

    def on_bar(self, aid: str, closes: List[float], px: float, ts: float) -> None:
        kind = self.b["kind"]
        p = self.pos(aid)
        if kind == "ma_cross":
            if len(closes) < 22:
                return
            f1, s1 = _sma(closes, 9), _sma(closes, 21)
            f0, s0 = _sma(closes[:-1], 9), _sma(closes[:-1], 21)
            if f0 <= s0 and f1 > s1 and not p:
                self.buy(aid, self.alloc(), px, ts, why="9/21 golden cross")
            elif f0 >= s0 and f1 < s1 and p:
                self.sell(p, px, ts, why="9/21 death cross")
        elif kind == "rsi_dip":
            r = _rsi(closes)
            if r is None:
                return
            if r < 30 and not p:
                self.buy(aid, self.alloc(), px, ts, why="RSI %.0f oversold" % r)
            elif r > 70 and p:
                self.sell(p, px, ts, why="RSI %.0f overbought" % r)
        elif kind == "momentum":
            if len(closes) < 7 or p:
                return
            ch = closes[-1] / closes[-7] - 1
            if ch >= 0.015:
                if self.buy(aid, self.alloc(), px, ts, why="+%.1f%% momentum" % (ch * 100)):
                    self.st["peak"][aid] = px

    def save(self) -> None:
        if self.dirty:
            db.x("UPDATE bots SET state=?, last_run_ts=? WHERE id=?", (json.dumps(self.st), now(), self.b["id"]))


# =========================================================================== funds
def create_fund(mode: str, name: str, holdings: List[dict]) -> dict:
    account(mode)
    hs = []
    for h in holdings or []:
        if fetchers.asset(h.get("id", "")) and float(h.get("weight", 0)) > 0:
            hs.append({"id": h["id"], "weight": float(h["weight"])})
    if len(hs) < 2:
        raise GameError("A fund needs at least 2 holdings")
    tot = sum(h["weight"] for h in hs)
    for h in hs:
        h["weight"] = h["weight"] / tot
    fid = db.x("INSERT INTO funds(mode,name,holdings,invested,created_ts) VALUES(?,?,?,0,?)",
               (mode, (name or "My Fund")[:40], json.dumps(hs), now()))
    log_event(mode, "fund", "Created fund %s with %d holdings" % (name, len(hs)))
    check_achievements(mode)
    return {"ok": True, "fund_id": fid}


def invest_fund(fid: int, amount: float) -> dict:
    f = db.q1("SELECT * FROM funds WHERE id=?", (fid,))
    if not f:
        raise GameError("fund not found")
    mode = f["mode"]
    amount = float(amount)
    if amount < 10 or amount > account(mode)["cash"]:
        raise GameError("Not enough cash (min $10)")
    _sync(mode)
    hs = json.loads(f["holdings"])
    queued = 0
    for h in hs:
        usd = amount * h["weight"]
        if not market_open(mode, h["id"]):
            adj_cash(mode, -usd)
            db.x("INSERT INTO orders(mode,asset_id,kind,side,amount_usd,leverage,status,created_ts,note) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (mode, h["id"], "open", "long", usd, 1, "queued", now(), "fund:%d" % fid))
            queued += 1
            continue
        px = price(mode, h["id"])
        if not px:
            continue
        _open(mode, h["id"], "long", usd, 1.0, px, None, None, fund_id=fid, merge=True)
    db.x("UPDATE funds SET invested=invested+? WHERE id=?", (amount, fid))
    log_event(mode, "fund", "Invested %s in %s" % (money(amount), f["name"]), None, amount)
    return {"ok": True, "queued_legs": queued}


def withdraw_fund(fid: int, fraction: float = 1.0) -> dict:
    f = db.q1("SELECT * FROM funds WHERE id=?", (fid,))
    if not f:
        raise GameError("fund not found")
    mode = f["mode"]
    fraction = max(0.01, min(1.0, float(fraction)))
    total = 0.0
    for p in db.q("SELECT * FROM positions WHERE fund_id=?", (fid,)):
        if not market_open(mode, p["asset_id"]):
            db.x("INSERT INTO orders(mode,asset_id,kind,side,position_id,fraction,status,created_ts,note) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (mode, p["asset_id"], "close", "long", p["id"], fraction, "queued", now(),
                                               "fund:%d" % fid))
            continue
        px = price(mode, p["asset_id"]) or p["entry"]
        total += _close(p, px, fraction, "fund")["pnl"]
    db.x("UPDATE funds SET invested=invested*? WHERE id=?", (1 - fraction, fid))
    if fraction >= 1 and not db.q1("SELECT 1 FROM positions WHERE fund_id=?", (fid,)):
        db.x("DELETE FROM funds WHERE id=?", (fid,))
    log_event(mode, "fund", "Withdrew %d%% of %s (P&L %s)" % (round(fraction * 100), f["name"], money(total)))
    return {"ok": True, "pnl": total}


# =========================================================================== settlement
import collections  # noqa: E402
_settle_locks: Dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
_last_settle_run: Dict[str, float] = collections.defaultdict(float)


def _active_assets(mode: str) -> List[str]:
    ids = {r["asset_id"] for r in db.q("SELECT DISTINCT asset_id FROM positions WHERE mode=?", (mode,))}
    ids |= {r["asset_id"] for r in db.q("SELECT DISTINCT asset_id FROM orders WHERE mode=? AND status='queued'", (mode,))}
    for b in db.q("SELECT tickers FROM bots WHERE mode=? AND active=1", (mode,)):
        ids |= set(json.loads(b["tickers"]))
    return sorted(ids)


def _check_position(p: dict, c: dict, ts: float) -> Optional[str]:
    """Apply stop loss / take profit / liquidation / margin call to one position over one candle."""
    o, h, l = c["open"], c["high"], c["low"]
    long_ = p["side"] == "long"
    risky = p["leverage"] > 1 or not long_
    liq = None
    if risky and p["qty"] > 0:
        dist = config.LIQUIDATION_RATIO * p["margin"] / p["qty"]
        liq = p["entry"] - dist if long_ else p["entry"] + dist
    mode = p["mode"]
    s = sym(p["asset_id"])
    # adverse triggers (stop / liquidation): first one touched wins
    if long_:
        adverse = [(x, k) for x, k in ((p["sl"], "stop"), (liq, "liquidation")) if x is not None and l <= x]
        adverse.sort(key=lambda t: -t[0])
        fav = p["tp"] is not None and h >= p["tp"]
    else:
        adverse = [(x, k) for x, k in ((p["sl"], "stop"), (liq, "liquidation")) if x is not None and h >= x]
        adverse.sort(key=lambda t: t[0])
        fav = p["tp"] is not None and l <= p["tp"]
    if fav and p["tp"] is not None and ((long_ and o >= p["tp"]) or (not long_ and o <= p["tp"])):
        adverse = []  # gapped straight through the take-profit
    if adverse:
        lvl, kind = adverse[0]
        fill = min(o, lvl) if long_ else max(o, lvl)
        if kind == "liquidation":
            r = _close(p, fill, 1.0, "liquidation", ts)
            log_event(mode, "liquidation", "LIQUIDATED %s %s at %s: lost %s of margin" % (p["side"].upper(), s, money(fill), money(-r["pnl"])),
                      p["asset_id"], r["pnl"], ts)
        else:
            r = _close(p, fill, 1.0, "stop", ts)
            log_event(mode, "stop", "Stop loss hit on %s %s at %s: %s" % (p["side"].upper(), s, money(fill), money(r["pnl"])),
                      p["asset_id"], r["pnl"], ts)
        return kind
    if fav:
        fill = max(o, p["tp"]) if long_ else min(o, p["tp"])
        r = _close(p, fill, 1.0, "tp", ts)
        log_event(mode, "tp", "Take profit hit on %s %s at %s: %s" % (p["side"].upper(), s, money(fill), money(r["pnl"])),
                  p["asset_id"], r["pnl"], ts)
        return "tp"
    if risky:
        worst = l if long_ else h
        loss_ratio = -upnl(p, worst) / p["margin"] if p["margin"] else 0
        if loss_ratio >= config.MARGIN_CALL_RATIO and not p["warned"]:
            db.x("UPDATE positions SET warned=1 WHERE id=?", (p["id"],))
            log_event(mode, "margin_call", "MARGIN CALL on %s %s: down %d%% of margin. Add margin or close to avoid liquidation."
                      % (p["side"].upper(), s, round(loss_ratio * 100)), p["asset_id"], 0, ts)
        elif loss_ratio < 0.4 and p["warned"]:
            db.x("UPDATE positions SET warned=0 WHERE id=?", (p["id"],))
            _grant("survivor", mode)
    return None


def _fill_queued(mode: str, aid: str, c: dict, ts: float) -> None:
    for o in db.q("SELECT * FROM orders WHERE mode=? AND asset_id=? AND status='queued' ORDER BY id", (mode, aid)):
        if not market_open(mode, aid, c["time"] + 1):
            return
        px = c["open"]
        if o["kind"] == "open":
            sl, tp = o["sl"], o["tp"]
            try:
                _validate_stops(o["side"], px, sl, tp)
            except GameError:
                sl = tp = None
            fund_id = int(o["note"].split(":")[1]) if (o["note"] or "").startswith("fund:") else None
            lev = o["leverage"] or 1
            _open(mode, aid, o["side"], o["amount_usd"], lev, px, sl, tp, fund_id=fund_id, merge=bool(fund_id),
                  ts=ts, reserved=o["amount_usd"] / lev)
            log_event(mode, "order_filled", "Queued order filled at the open: %s %s %s at %s" %
                      (o["side"].upper(), money(o["amount_usd"]), sym(aid), money(px)), aid, o["amount_usd"], ts)
        else:
            p = db.q1("SELECT * FROM positions WHERE id=?", (o["position_id"],))
            if p:
                r = _close(p, px, o["fraction"] or 1.0, "fund" if p["fund_id"] else "manual", ts)
                log_event(mode, "order_filled", "Queued close filled at the open: %s at %s: %s" %
                          (sym(aid), money(px), money(r["pnl"])), aid, r["pnl"], ts)
        db.x("UPDATE orders SET status='filled', filled_ts=?, fill_price=? WHERE id=?", (ts, px, o["id"]))


def _process(mode: str, series: Dict[str, List[dict]], real_ts_from_candle: bool) -> int:
    """Walk candles of all active assets in time order, applying orders, stops, bots and turbo dividends."""
    events = []
    for aid, cs in series.items():
        for c in cs:
            events.append((c.get("end", c["time"]), aid, c))
    events.sort(key=lambda e: e[0])
    if not events:
        return 0
    bots = [_BotRun(b) for b in db.q("SELECT * FROM bots WHERE mode=? AND active=1", (mode,))]
    t_now = now()
    n = 0
    with db.tx():
        for t_end, aid, c in events:
            ts = min(t_end, t_now) if real_ts_from_candle else t_now
            if is_live(mode) and is_stock(aid):
                _fill_queued(mode, aid, c, ts)
            for p in db.q("SELECT * FROM positions WHERE mode=? AND asset_id=?", (mode, aid)):
                if _check_position(p, c, ts):
                    n += 1
            if mode == "turbo" and is_stock(aid):
                _turbo_dividends(aid, c)
            for b in bots:
                b.feed(aid, c, ts)
        for b in bots:
            b.save()
    return n


def _turbo_dividends(aid: str, c: dict) -> None:
    y = sim.dividend_yield(aid)
    if y <= 0:
        return
    for p in db.q("SELECT * FROM positions WHERE mode='turbo' AND asset_id=?", (aid,)):
        last = p["last_div_ts"] or c["time"]
        if c["end"] - last >= 91 * 86400:
            amt = p["qty"] * c["close"] * y / 4 * (1 if p["side"] == "long" else -1)
            _pay_div(p, amt, "turbo quarterly dividend (%.2f%% yield)" % (y * 100), now())
            db.x("UPDATE positions SET last_div_ts=? WHERE id=?", (last + 91 * 86400, p["id"]))


def _pay_div(p: dict, amt: float, why: str, ts: float) -> None:
    if p["bot_id"]:
        db.x("UPDATE bots SET cash=cash+? WHERE id=?", (amt, p["bot_id"]))
    else:
        adj_cash(p["mode"], amt)
    db.x("UPDATE accounts SET dividends=dividends+? WHERE mode=?", (amt, p["mode"]))
    if amt >= 0:
        txt = "Dividend: %s paid %s on %s shares (%s)" % (sym(p["asset_id"]), money(amt), "{:,.4g}".format(p["qty"]), why)
    else:
        txt = "Dividend charged on SHORT %s: %s (%s)" % (sym(p["asset_id"]), money(amt), why)
    log_event(p["mode"], "dividend", txt, p["asset_id"], amt, ts)


def _live_dividends(mode: str = "live", force: bool = False) -> None:
    t = now()
    if not force and t - (db.kv_get("div_check_ts:" + mode, 0) or 0) < 1800:
        return
    db.kv_set("div_check_ts:" + mode, t)
    pos = db.q("SELECT * FROM positions WHERE mode=? AND asset_id LIKE 'S:%'", (mode,))
    for aid in {p["asset_id"] for p in pos}:
        divs = fetchers.get_dividends(aid)
        if not divs:
            continue
        with db.tx():
            for p in db.q("SELECT * FROM positions WHERE mode=? AND asset_id=?", (mode, aid)):
                last = p["last_div_ts"] or p["opened_ts"]
                paid_to = last
                for d in divs:
                    if last < d["ts"] <= t and p["opened_ts"] < d["ts"]:
                        amt = d["amount"] * p["qty"] * (1 if p["side"] == "long" else -1)
                        _pay_div(p, amt, "ex-date %s, $%.4f/share" % (time.strftime("%b %d", time.gmtime(d["ts"])), d["amount"]), d["ts"])
                        paid_to = max(paid_to, d["ts"])
                db.x("UPDATE positions SET last_div_ts=? WHERE id=?", (max(paid_to, t), p["id"]))


def settle(mode: str, force: bool = False) -> dict:
    """Bring a mode up to date. Live: replays REAL candles since the last settlement (offline catch-up),
    or uses fresh quotes for short gaps. Turbo: advances the simulator to the current simulated time."""
    lock = _settle_locks[mode]
    if not lock.acquire(blocking=force):
        return {"skipped": True}
    try:
        acc = account(mode)
        t = now()
        min_gap = 1.0 if mode == "turbo" else 8.0
        if not force and t - _last_settle_run[mode] < min_gap:
            return {"skipped": True}
        _last_settle_run[mode] = t
        give_starter(mode)
        last = acc["last_settle_ts"] or t
        assets = _active_assets(mode)
        hits = 0
        if mode == "turbo":
            skipped = sim.cap_offline(last, t)
            target = sim.sim_now(t)
            series = {}
            for aid in assets:
                sim.advance(aid, target)
                series[aid] = sim.drain(aid)
            hits = _process(mode, series, real_ts_from_candle=False)
            db.x("UPDATE accounts SET sim_ts=? WHERE mode='turbo'", (target,))
        else:
            gap = t - last
            series = {}
            if gap > 120 and assets:
                for aid in assets:
                    try:
                        series[aid] = fetchers.get_candles_between(aid, last, t)
                    except Exception:
                        series[aid] = []
            qs = fetchers.get_quotes(assets) if assets else {}
            for aid in assets:
                q = qs.get(aid)
                if not q or q.get("stale") and q.get("source") == "cache":
                    continue
                if is_stock(aid) and follows_hours(mode) and not markethours.is_open(t):
                    # closed: no ticks, but queued orders wait; stops can't trigger outside the session
                    continue
                px = q["price"]
                series.setdefault(aid, []).append({"time": int(t), "end": int(t), "open": px, "high": px, "low": px,
                                                   "close": px, "volume": 0})
            hits = _process(mode, series, real_ts_from_candle=True)
            try:
                _live_dividends(mode, force=gap > 3600)
            except Exception as e:
                log.warning("dividend check failed: %s", e)
        db.x("UPDATE accounts SET last_settle_ts=? WHERE mode=?", (t, mode))
        _snapshot(mode)
        check_achievements(mode)
        return {"ok": True, "hits": hits}
    finally:
        lock.release()


def _snapshot(mode: str) -> None:
    t = now()
    last = db.q1("SELECT ts FROM networth WHERE mode=? ORDER BY ts DESC LIMIT 1", (mode,))
    every = 60 if mode == "turbo" else 300
    if last and t - last["ts"] < every:
        return
    nw = summary(mode, quick=True)["networth"]
    db.x("INSERT INTO networth(mode,ts,value) VALUES(?,?,?)", (mode, t, nw))


# =========================================================================== views
def positions_view(mode: str) -> List[dict]:
    ps = db.q("SELECT * FROM positions WHERE mode=? ORDER BY opened_ts", (mode,))
    qs = quotes(mode, list({p["asset_id"] for p in ps})) if ps else {}
    out = []
    for p in ps:
        q = qs.get(p["asset_id"])
        px = q["price"] if q else p["entry"]
        pl = upnl(p, px)
        a = fetchers.asset(p["asset_id"]) or {}
        risky = p["leverage"] > 1 or p["side"] == "short"
        risk = max(0.0, -pl / p["margin"]) if (risky and p["margin"]) else 0.0
        liq = None
        if risky and p["qty"]:
            dist = config.LIQUIDATION_RATIO * p["margin"] / p["qty"]
            liq = p["entry"] - dist if p["side"] == "long" else p["entry"] + dist
        out.append(dict(p, price=px, upnl=pl, pnl_pct=pl / p["margin"] * 100 if p["margin"] else 0,
                        value=p["margin"] + pl, notional=p["qty"] * px, risk=risk, liq_price=liq,
                        symbol=(a.get("symbol") or p["asset_id"][2:]).upper(), name=a.get("name"),
                        kind=a.get("kind"), logo=a.get("logo"), stale=bool(q and q.get("stale"))))
    return out


def summary(mode: str, quick: bool = False) -> dict:
    acc = account(mode)
    ps = positions_view(mode)
    bots = db.q("SELECT cash FROM bots WHERE mode=?", (mode,))
    reserved = sum((o["amount_usd"] or 0) / (o["leverage"] or 1)
                   for o in db.q("SELECT * FROM orders WHERE mode=? AND status='queued' AND kind='open'", (mode,)))
    pos_val = sum(p["value"] for p in ps)
    bot_cash = sum(b["cash"] for b in bots)
    nw = acc["cash"] + pos_val + bot_cash + reserved
    out = {"cash": acc["cash"], "networth": nw, "positions_value": pos_val, "bot_cash": bot_cash,
           "reserved": reserved, "upnl": sum(p["upnl"] for p in ps), "realized": acc["realized"],
           "dividends": acc["dividends"], "used_margin": sum(p["margin"] for p in ps)}
    if not quick:
        out["positions"] = ps
    return out


def state(mode: str, hello: bool = False) -> dict:
    acc = account(mode)
    t = now()
    away = None
    if hello and t - (acc["last_seen_ts"] or t) > config.AWAY_THRESHOLD:
        away = away_summary(mode, acc["last_seen_ts"])
    db.x("UPDATE accounts SET last_seen_ts=? WHERE mode=?", (t, mode))
    s = summary(mode)
    warn = [p for p in s["positions"] if p["risk"] >= config.MARGIN_CALL_RATIO * 0.7]
    orders = db.q("SELECT * FROM orders WHERE mode=? AND status='queued' ORDER BY id", (mode,))
    for o in orders:
        o["symbol"] = sym(o["asset_id"])
    toasts = db.kv_get("toasts:" + mode, []) or []
    if toasts:
        db.kv_set("toasts:" + mode, [])
    return {"mode": mode, "account": {k: v for k, v in s.items() if k != "positions"}, "positions": s["positions"],
            "orders": orders, "profile": profile(mode),
            "market": dict(markethours.status(t), hours_on=follows_hours(mode)),
            "warnings": [{"id": p["id"], "symbol": p["symbol"], "risk": p["risk"], "liq_price": p["liq_price"],
                          "side": p["side"]} for p in warn],
            "away": away, "toasts": toasts, "turbo": {"speed": sim.speed(), "sim_ts": sim.sim_now(t)} if mode == "turbo" else None,
            "server_ts": t}


def away_summary(mode: str, since: float) -> Optional[dict]:
    ev = db.q("SELECT * FROM events WHERE mode=? AND ts>? ORDER BY ts", (mode, since))
    def tot(tp):
        return sum(e["amount"] for e in ev if e["type"] == tp)
    nw_then = db.q1("SELECT value FROM networth WHERE mode=? AND ts<=? ORDER BY ts DESC LIMIT 1", (mode, since))
    nw_now = summary(mode, quick=True)["networth"]
    out = {"since": since, "seconds": now() - since,
           "dividends": tot("dividend"), "dividend_count": sum(1 for e in ev if e["type"] == "dividend"),
           "stops": [e["text"] for e in ev if e["type"] == "stop"],
           "take_profits": [e["text"] for e in ev if e["type"] == "tp"],
           "liquidations": [e["text"] for e in ev if e["type"] == "liquidation"],
           "margin_calls": [e["text"] for e in ev if e["type"] == "margin_call"],
           "filled_orders": [e["text"] for e in ev if e["type"] == "order_filled"],
           "bot_trades": sum(1 for e in ev if e["type"] == "bot_trade"),
           "bot_log": [e["text"] for e in ev if e["type"] == "bot_trade"][-8:],
           "realized": sum(e["amount"] for e in ev if e["type"] in ("stop", "tp", "liquidation")),
           "networth_then": nw_then["value"] if nw_then else None, "networth_now": nw_now}
    out["change"] = (nw_now - out["networth_then"]) if out["networth_then"] is not None else None
    return out


def bots_view(mode: str) -> dict:
    bots = db.q("SELECT * FROM bots WHERE mode=? ORDER BY id", (mode,))
    out = []
    for b in bots:
        ps = [p for p in positions_view(mode) if p["bot_id"] == b["id"]]
        val = b["cash"] + sum(p["value"] for p in ps)
        out.append({"id": b["id"], "kind": b["kind"], "name": b["name"], "tickers": json.loads(b["tickers"]),
                    "symbols": [sym(t) for t in json.loads(b["tickers"])], "budget": b["budget"], "cash": b["cash"],
                    "value": val, "pnl": val - b["budget"], "active": bool(b["active"]), "trades": b["trades"],
                    "params": json.loads(b["params"] or "{}"), "positions": len(ps)})
    log_ = db.q("SELECT * FROM events WHERE mode=? AND type='bot_trade' ORDER BY ts DESC LIMIT 30", (mode,))
    return {"bots": out, "types": config.BOT_TYPES, "unlocked": unlocked("bots"), "log": log_}


def funds_view(mode: str) -> dict:
    fs = db.q("SELECT * FROM funds WHERE mode=? ORDER BY id", (mode,))
    pv = positions_view(mode)
    out = []
    for f in fs:
        ps = [p for p in pv if p["fund_id"] == f["id"]]
        val = sum(p["value"] for p in ps)
        hs = json.loads(f["holdings"])
        for h in hs:
            h["symbol"] = sym(h["id"])
        out.append({"id": f["id"], "name": f["name"], "holdings": hs, "invested": f["invested"], "value": val,
                    "pnl": val - f["invested"], "pnl_pct": (val / f["invested"] - 1) * 100 if f["invested"] else 0})
    return {"funds": out, "unlocked": unlocked("funds")}


# =========================================================================== achievements & stats
ACHIEVEMENTS = [
    ("first_trade", "First Trade", "Open your first position"),
    ("first_profit", "In the Green", "Close a trade in profit"),
    ("first_short", "Bear Mode", "Open a short position"),
    ("stop_saver", "Saved by the Stop", "Have a stop loss protect you"),
    ("tp_hit", "Locked In", "Hit a take-profit"),
    ("ten_trades", "Active Trader", "Close 10 trades"),
    ("fifty_trades", "Market Regular", "Close 50 trades"),
    ("diversified", "Diversified", "Hold 5 different tickers at once"),
    ("dividend", "Passive Income", "Receive a dividend"),
    ("leverage", "Leveraged Up", "Open a leveraged trade"),
    ("survivor", "Survivor", "Recover from a margin call"),
    ("crypto", "To the Moon", "Trade a cryptocurrency"),
    ("bot_owner", "Automation", "Buy a trading bot"),
    ("fund_manager", "Fund Manager", "Create your own fund"),
    ("nw_20k", "$20K Club", "Reach $20,000 net worth"),
    ("nw_50k", "$50K Club", "Reach $50,000 net worth"),
    ("nw_100k", "Tycoon", "Reach $100,000 net worth"),
    ("level5", "Rising Star", "Reach level 5"),
    ("level10", "Pro Trader", "Reach level 10"),
    ("scholar", "Scholar", "Complete every lesson"),
    ("quiz_whiz", "Quiz Whiz", "Pass every quiz"),
]


def _grant(aid: str, mode: str = "live") -> bool:
    if db.q1("SELECT 1 FROM achievements WHERE owner=? AND id=?", (mode, aid)):
        return False
    db.x("INSERT INTO achievements(owner,id,ts) VALUES(?,?,?)", (mode, aid, now()))
    name = dict((a[0], a[1]) for a in ACHIEVEMENTS).get(aid, aid)
    pend = db.kv_get("toasts:" + mode, []) or []
    pend.append({"type": "achievement", "text": "Achievement unlocked: " + name})
    db.kv_set("toasts:" + mode, pend[-10:])
    return True


def check_achievements(mode: str) -> None:
    try:
        M = (mode,)
        n_tr = db.q1("SELECT COUNT(*) n FROM trades WHERE mode=? AND bot_id IS NULL AND fund_id IS NULL", M)["n"]
        g = lambda k: _grant(k, mode)  # noqa: E731
        if n_tr or db.q1("SELECT 1 FROM events WHERE mode=? AND type IN ('open','order_queued')", M):
            g("first_trade")
        if db.q1("SELECT 1 FROM trades WHERE mode=? AND pnl>0 AND bot_id IS NULL", M):
            g("first_profit")
        if db.q1("SELECT 1 FROM positions WHERE mode=? AND side='short'", M) or db.q1("SELECT 1 FROM trades WHERE mode=? AND side='short'", M):
            g("first_short")
        if db.q1("SELECT 1 FROM trades WHERE mode=? AND reason='stop'", M):
            g("stop_saver")
        if db.q1("SELECT 1 FROM trades WHERE mode=? AND reason='tp'", M):
            g("tp_hit")
        if n_tr >= 10:
            g("ten_trades")
        if n_tr >= 50:
            g("fifty_trades")
        if db.q1("SELECT COUNT(DISTINCT asset_id) n FROM positions WHERE mode=?", M)["n"] >= 5:
            g("diversified")
        if db.q1("SELECT 1 FROM events WHERE mode=? AND type='dividend' AND amount>0", M):
            g("dividend")
        if db.q1("SELECT 1 FROM positions WHERE mode=? AND leverage>1", M) or db.q1("SELECT 1 FROM trades WHERE mode=? AND leverage>1", M):
            g("leverage")
        if db.q1("SELECT 1 FROM positions WHERE mode=? AND asset_id LIKE 'C:%' AND bot_id IS NULL", M) or \
                db.q1("SELECT 1 FROM trades WHERE mode=? AND asset_id LIKE 'C:%'", M):
            g("crypto")
        if db.q1("SELECT 1 FROM bots WHERE mode=?", M):
            g("bot_owner")
        if db.q1("SELECT 1 FROM funds WHERE mode=?", M):
            g("fund_manager")
        nw = summary(mode, quick=True)["networth"]
        for k, v in (("nw_20k", 20000), ("nw_50k", 50000), ("nw_100k", 100000)):
            if nw >= v:
                g(k)
        lv = profile(mode)["level"]
        if lv >= 5:
            g("level5")
        if lv >= 10:
            g("level10")
    except Exception as e:  # never let achievements break gameplay
        log.warning("achievements: %s", e)


def stats(mode: str) -> dict:
    tr = db.q("SELECT * FROM trades WHERE mode=? ORDER BY closed_ts", (mode,))
    manual = [t for t in tr if not t["bot_id"] and not t["fund_id"]]
    wins = [t for t in manual if t["pnl"] > 0]
    nw = db.q("SELECT ts, value FROM networth WHERE mode=? ORDER BY ts", (mode,))
    if len(nw) > 500:
        step = len(nw) / 500
        nw = [nw[int(i * step)] for i in range(500)] + [nw[-1]]
    s = summary(mode, quick=True)
    nw.append({"ts": now(), "value": s["networth"]})
    best = max(manual, key=lambda t: t["pnl"]) if manual else None
    worst = min(manual, key=lambda t: t["pnl"]) if manual else None
    for t in (best, worst):
        if t:
            t["symbol"] = sym(t["asset_id"])
    got = {r["id"]: r["ts"] for r in db.q("SELECT * FROM achievements WHERE owner=?", (mode,))}
    by_reason: Dict[str, int] = {}
    for t in manual:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    recent = [dict(t, symbol=sym(t["asset_id"])) for t in tr[-25:][::-1]]
    return {"networth": nw, "trades": len(manual), "wins": len(wins), "win_rate": len(wins) / len(manual) * 100 if manual else 0,
            "best": best, "worst": worst, "realized": sum(t["pnl"] for t in tr),
            "dividends": account(mode)["dividends"], "by_reason": by_reason, "bot_trades": sum(1 for t in tr if t["bot_id"]),
            "achievements": [{"id": a, "name": n, "desc": d, "ts": got.get(a)} for a, n, d in ACHIEVEMENTS],
            "profile": profile(mode), "recent": recent, "start_cash": config.START_CASH, "current": s}


def reset(mode: str) -> None:
    with db.tx():
        for tbl in ("positions", "orders", "trades", "events", "networth", "bots", "funds"):
            db.conn().execute("DELETE FROM %s WHERE mode=?" % tbl, (mode,))
        db.conn().execute("DELETE FROM accounts WHERE mode=?", (mode,))
        db.conn().execute("DELETE FROM codes WHERE mode=?", (mode,))
    if mode == "turbo":
        sim.reset()
    account(mode)


# =========================================================================== background loop
def active_owners() -> List[str]:
    rows = db.q("SELECT DISTINCT mode FROM positions UNION SELECT DISTINCT mode FROM orders WHERE status='queued' "
                "UNION SELECT DISTINCT mode FROM bots WHERE active=1")
    return [r["mode"] for r in rows if r["mode"] != "turbo"]


def background_loop(stop: threading.Event) -> None:
    """Keeps every player's account running while the server is up: stops, take-profits, bots, dividends."""
    while not stop.is_set():
        for m in active_owners():
            if stop.is_set():
                break
            try:
                settle(m)
            except Exception as e:
                log.warning("background settle %s failed: %s", m, e)
        stop.wait(15)
