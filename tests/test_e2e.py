"""End-to-end test of the game API with offline fixtures (real response formats, no network needed).

    TT_FIXTURES=1 python -m tests.test_e2e      (run from the project folder)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["TT_FIXTURES"] = "1"
os.environ["TT_DEBUG"] = "1"
os.environ["TT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["YOUTUBE_API_KEY"] = "test-key"

import app as appmod  # noqa: E402
import engine  # noqa: E402
import fetchers  # noqa: E402
import markethours  # noqa: E402

appmod.startup_db_only = True
import db as _db  # noqa: E402
import auth as _auth  # noqa: E402
_db.conn()
appmod.app.secret_key = _auth.secret_key()
c = appmod.app.test_client()
OK = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra else ""))
    OK.append(bool(cond))


def get(url):
    r = c.get(url)
    return r.status_code, r.get_json()


def post(url, js):
    r = c.post(url, json=js)
    return r.status_code, r.get_json()


# ---------------------------------------------------------------- accounts & login
r = c.get("/")
check("not logged in: / redirects to the login page", r.status_code == 302 and "/login" in r.headers["Location"])
r = c.get("/api/state")
check("not logged in: API answers 401", r.status_code == 401)
check("login page renders", c.get("/login").status_code == 200)
code, js = post("/api/auth/signup", {"username": "dean", "password": "secret123"})
check("sign up first player (admin)", code == 200, js)
OWNER = "u1"
code, js = post("/api/auth/signup", {"username": "DEAN", "password": "secret123"})
check("usernames are unique (case-insensitive)", code == 400)
post("/api/auth/logout", {})
code, js = post("/api/auth/login", {"username": "dean", "password": "wrong"})
check("wrong password rejected", code == 400)
code, js = post("/api/auth/login", {"username": "dean", "password": "secret123"})
check("log in again (works from any computer)", code == 200)

# ---------------------------------------------------------------- symbols & search
info = fetchers.load_symbols(force=True)
check("symbol lists downloaded (stocks + coins)", info["stocks_downloaded"] > 1000 and info["coins_downloaded"] > 500, info)
check("test issues excluded", not fetchers.asset("S:ZXZZT") and not fetchers.asset("S:NTEST"))
for q, want in (("AAPL", "S:AAPL"), ("nvidia", "S:NVDA"), ("NVDA", "S:NVDA"), ("btc", "C:bitcoin"),
                ("bitcoin", "C:bitcoin"), ("DOGE", "C:dogecoin"), ("audioeye", "S:AEYE"), ("aeye", "S:AEYE")):
    _, js = get("/api/search?q=" + q)
    top = js["results"][0]["id"] if js["results"] else None
    check("search %-9s -> %s" % (q, want), top == want, top)
_, js = get("/api/search?q=nvdia")
check("fuzzy search typo 'nvdia' finds NVDA", any(r["id"] == "S:NVDA" for r in js["results"][:5]))
_, js = get("/api/search?filter=etfs")
check("ETF filter", js["results"] and all(r["kind"] == "etf" for r in js["results"]))
_, js = get("/api/search?filter=crypto&q=b")
check("crypto filter", js["results"] and all(r["kind"] == "crypto" for r in js["results"]))
_, js = get("/api/search?filter=movers")
check("top movers", len(js["results"]) > 0, [r["symbol"] for r in js["results"][:5]])
check("BRK.A maps to Yahoo BRK-A", fetchers.asset("S:BRK.A")["yahoo"] == "BRK-A")

# ---------------------------------------------------------------- quotes & candles
_, js = get("/api/quotes?ids=S:AAPL,C:bitcoin")
check("live quotes stock+crypto", js["S:AAPL"]["price"] == 230.0 and js["C:bitcoin"]["price"] == 100000.0)
for tf in ("1m", "5m", "1h", "1D", "1W"):
    _, js = get("/api/candles?id=S:AAPL&tf=" + tf)
    check("candles AAPL " + tf, len(js["candles"]) > 10)
_, js = get("/api/candles?id=C:dogecoin&tf=1m")
check("crypto candles via Yahoo (validated vs CoinGecko)", len(js["candles"]) > 10)

# ---------------------------------------------------------------- live trading (force market open)
real_open = markethours.is_open
markethours.is_open = lambda ts: True
_, st = get("/api/state?mode=live&hello=1")
check("start cash $10,000 + free starter position", abs(st["account"]["cash"] - 10000) < 1e-6 and
      any(p["symbol"] == "SPY" for p in st["positions"]), st["account"]["cash"])
code, js = post("/api/redeem", {"mode": "live", "code": "WELCOME"})
check("redeem WELCOME +$5,000", code == 200, js)
code, js = post("/api/redeem", {"mode": "live", "code": "WELCOME"})
check("WELCOME only once", code == 400)
_, st = get("/api/state?mode=live")
check("cash now $15,000", abs(st["account"]["cash"] - 15000) < 1e-6)

code, js = post("/api/order", {"mode": "live", "id": "S:AAPL", "side": "long", "amount": 1000, "sl": 220, "tp": 250})
check("open LONG AAPL $1,000 with SL/TP", code == 200 and not js["queued"], js)
long_id = js["position_id"]
code, js = post("/api/order", {"mode": "live", "id": "S:NVDA", "side": "short", "amount": 500})
check("open SHORT NVDA $500", code == 200, js)
short_id = js["position_id"]
_, st0 = get("/api/state?mode=live")
check("nothing locked: 10x leverage available at level 1", st0["profile"]["max_leverage"] == 10 and st0["profile"]["rank"] == "Rookie")
code, js = post("/api/order", {"mode": "live", "id": "S:AAPL", "side": "long", "amount": 100, "sl": 240})
check("invalid stop (above price for long) rejected", code == 400)

fetchers._qcache["S:AAPL"] = {"price": 241.5, "prev_close": 230, "change_pct": 5, "ts": time.time(), "source": "test"}
code, js = post("/api/position/%d/close" % long_id, {"fraction": 0.5})
check("partial close 50% of long", code == 200 and abs(js["pnl"] - (241.5 - 230) * (1000 / 230) * 0.5) < 0.01, js)
code, js = post("/api/position/%d/close" % long_id, {"fraction": 1})
check("close rest of long with profit + XP", code == 200 and js["pnl"] > 0 and js["xp"] > 0, js)
fetchers._qcache["S:NVDA"] = {"price": 171.0, "prev_close": 180, "change_pct": -5, "ts": time.time(), "source": "test"}
code, js = post("/api/position/%d/close" % short_id, {})
check("close short in profit when price fell", code == 200 and abs(js["pnl"] - (180 - 171) * (500 / 180)) < 0.01, js)
fetchers._qcache.clear()

# ---------------------------------------------------------------- stop triggers (live tick)
code, js = post("/api/order", {"mode": "live", "id": "S:TSLA", "side": "long", "amount": 800, "sl": 240})
pid = js["position_id"]
fetchers._qcache["S:TSLA"] = {"price": 238.0, "prev_close": 250, "change_pct": -4.8, "ts": time.time(), "source": "test"}
engine.settle(OWNER, force=True)
tr = appmod.db.q("SELECT * FROM trades WHERE asset_id='S:TSLA'")
check("stop loss triggers on live tick", tr and tr[-1]["reason"] == "stop" and abs(tr[-1]["exit"] - 238.0) < 1e-6,
      tr[-1] if tr else None)
fetchers._qcache.clear()

# ---------------------------------------------------------------- offline settlement: 2 hours away
code, js = post("/api/order", {"mode": "live", "id": "C:dogecoin", "side": "long", "amount": 1000, "sl": 0.19})
check("open DOGE long with stop 0.19", code == 200, js)
code, js = post("/api/order", {"mode": "live", "id": "S:KO", "side": "long", "amount": 2100})
check("open KO long (dividend stock)", code == 200, js)
ko_qty = 2100 / 70.0
appmod.db.kv_set("div_check_ts", 0)
appmod.db.x("UPDATE positions SET last_div_ts=NULL")
code, js = post("/api/dev/timewarp", {"mode": "live", "hours": 2})
check("simulate 2 hours away", code == 200)
_, st = get("/api/state?mode=live&hello=1")
away = st["away"]
check("'While you were away' summary returned", away is not None and away["seconds"] > 7000, away and away["seconds"])
check("offline: DOGE stop hit during the 1h-ago dip", away and any("DOGE" in s for s in away["stops"]), away and away["stops"])
check("offline: real KO dividend paid ($0.51/share)", away and abs(away["dividends"] - ko_qty * 0.51) < 0.01,
      away and away["dividends"])
markethours.is_open = real_open

# ---------------------------------------------------------------- market closed -> queue
markethours.is_open = lambda ts: False
code, js = post("/api/order", {"mode": "live", "id": "S:AMD", "side": "long", "amount": 300})
check("stock order queued when market closed", code == 200 and js["queued"], js)
code, js = post("/api/order", {"mode": "live", "id": "C:ethereum", "side": "short", "amount": 300})
check("crypto still trades 24/7 when stocks closed", code == 200 and not js["queued"], js)
markethours.is_open = lambda ts: True
fetchers._qcache.clear()
engine.settle(OWNER, force=True)
filled = appmod.db.q1("SELECT * FROM orders WHERE asset_id='S:AMD'")
check("queued order fills at the open", filled["status"] == "filled", filled["status"])

# ---------------------------------------------------------------- margin call & liquidation
post("/api/dev/xp", {"xp": 5000})
_, st = get("/api/state?mode=live")
check("XP raises level and rank", st["profile"]["level"] >= 10 and st["profile"]["rank"] != "Rookie", st["profile"]["rank"])
code, js = post("/api/order", {"mode": "live", "id": "C:bitcoin", "side": "long", "amount": 5000, "leverage": 10})
lev_id = js["position_id"]
fetchers._qcache["C:bitcoin"] = {"price": 96500.0, "prev_close": 100000, "change_pct": -3.5, "ts": time.time(), "source": "test"}
engine.settle(OWNER, force=True)
_, st = get("/api/state?mode=live")
check("margin call warning at -35% of margin (10x, -3.5%)", any(w["id"] == lev_id for w in st["warnings"]), st["warnings"])
fetchers._qcache["C:bitcoin"] = {"price": 90500.0, "prev_close": 100000, "change_pct": -9.5, "ts": time.time(), "source": "test"}
engine.settle(OWNER, force=True)
t = appmod.db.q1("SELECT * FROM trades WHERE asset_id='C:bitcoin' ORDER BY id DESC")
check("10x long liquidated after -9.5%", t and t["reason"] == "liquidation", t and t["reason"])
fetchers._qcache.clear()

# ---------------------------------------------------------------- bots & funds
code, js = post("/api/bots", {"mode": "live", "kind": "dca", "tickers": ["C:bitcoin"], "budget": 500,
                              "params": {"amount": 50, "interval": 60}})
check("buy DCA bot", code == 200, js)
code, js = post("/api/bots", {"mode": "live", "kind": "grid", "tickers": ["S:AAPL"], "budget": 500})
check("grid bot rejects stocks", code == 400)
appmod.db.x("UPDATE accounts SET last_settle_ts=last_settle_ts-600 WHERE mode=?", (OWNER,))
engine.settle(OWNER, force=True)
_, js = get("/api/bots?mode=live")
check("DCA bot traded automatically", js["bots"][0]["trades"] >= 1, js["bots"][0])
code, js = post("/api/funds", {"mode": "live", "name": "Tech + BTC", "holdings": [{"id": "S:AAPL", "weight": 50},
                                                                              {"id": "C:bitcoin", "weight": 50}], "invest": 1000})
check("create + invest in fund", code == 200, js)
_, js = get("/api/funds?mode=live")
check("fund value ~ $1,000", abs(js["funds"][0]["value"] - 1000) < 5, js["funds"][0]["value"])

# ---------------------------------------------------------------- multiplayer isolation
dean_pos = appmod.db.q1("SELECT id FROM positions WHERE mode=? LIMIT 1", (OWNER,))["id"]
c2 = appmod.app.test_client()
r = c2.post("/api/auth/signup", json={"username": "maria", "password": "pass1234"})
check("second player signs up", r.status_code == 200)
st2 = c2.get("/api/state?hello=1").get_json()
check("second player has their own fresh $10,000 account", abs(st2["account"]["cash"] - 10000) < 1e-6 and
      all(p["mode"] == "u2" for p in st2["positions"]))
r = c2.post("/api/position/%d/close" % dean_pos, json={})
check("players can't touch each other's positions", r.status_code == 400 and
      appmod.db.q1("SELECT 1 FROM positions WHERE id=?", (dean_pos,)) is not None)
lb = c2.get("/api/leaderboard").get_json()
check("leaderboard ranks both players", lb["players"] == 2 and lb["me"]["username"] == "maria", [x["username"] for x in lb["top"]])
markethours.is_open = lambda ts: False
r = c2.post("/api/order", json={"id": "S:AAPL", "side": "long", "amount": 100})
check("market hours on: stock order queues while closed", r.get_json().get("queued") is True)
c2.post("/api/settings", json={"market_hours": False})
r = c2.post("/api/order", json={"id": "S:AAPL", "side": "long", "amount": 100})
check("market hours switched off in Settings: stock trades right away", r.status_code == 200 and r.get_json().get("queued") is False, r.get_json())
markethours.is_open = lambda ts: True

# ---------------------------------------------------------------- learn & news
_, js = get("/api/learn")
check("11 lessons in order", [l["id"] for l in js["lessons"]][:3] == ["stock", "crypto", "candles"] and js["total"] == 11)
_, js = get("/api/learn/videos?lesson=stops")
check("Learn loads YouTube videos (ranked, Shorts filtered)", js["source"] == "youtube" and 3 <= len(js["videos"]) <= 5 and
      all(v["duration"] >= 90 for v in js["videos"]), [v["id"] for v in js["videos"]])
_, js = get("/api/learn/search?q=what is a short squeeze")
check("Learn search box finds videos", js["videos"])
code, js = post("/api/learn/quiz", {"lesson": "stock", "answers": [1, 1, 2]})
check("quiz pass gives bonus XP", js["passed"] and js["xp"] > 0, js["xp"])
_, js = get("/api/news?id=S:AAPL")
check("news headlines", len(js["items"]) > 0, js.get("note"))
_, js = get("/api/stats?mode=live")
check("stats: win rate, best/worst, achievements", js["trades"] > 0 and js["best"] and
      any(a["ts"] for a in js["achievements"]), "%d trades, %.0f%% win" % (js["trades"], js["win_rate"]))

os.environ.pop("YOUTUBE_API_KEY")
import config  # noqa: E402
config.YOUTUBE_API_KEY = ""
_, js = get("/api/learn/videos?lesson=leverage")
check("without key: hand-picked fallback + 'add key' message", js["source"] == "fallback" and "YOUTUBE_API_KEY" in js["note"])

print("\n%d/%d checks passed" % (sum(OK), len(OK)))
sys.exit(0 if all(OK) else 1)
