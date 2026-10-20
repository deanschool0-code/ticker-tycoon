"""Ticker Tycoon - idle stock & crypto trading game with real market data.
Run:  bash run.sh   (or: python app.py)  ->  http://localhost:5558
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, session

import auth
import config
import db
import engine
import fetchers
import learn
import markethours
import sim

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tt")
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
try:
    app.json.sort_keys = False  # Flask >= 2.3
except Exception:  # pragma: no cover
    pass
try:  # correct client IP / https scheme behind the Cloudflare tunnel
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
except Exception:  # pragma: no cover
    pass
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_NAME="tt_session", PERMANENT_SESSION_LIFETIME=timedelta(days=60),
                  SESSION_COOKIE_SECURE=config.SECURE_COOKIES)
STOP = threading.Event()
_started = False
PUBLIC_API = ("/api/health", "/api/auth/")


def current_user():
    u = getattr(request, "_tt_user", None)
    if u is None:
        u = auth.user(session.get("uid")) or False
        request._tt_user = u
    return u or None


def mode_arg() -> str:
    """The logged-in player's account key ('u<id>'). Every game call is scoped to it."""
    u = current_user()
    if not u:
        raise engine.GameError("Please log in")
    return u["owner"]


def client_ip() -> str:
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or ""


def own(table: str, row_id: int) -> None:
    """Make sure the position/order/bot/fund belongs to the logged-in player."""
    r = db.q1("SELECT mode FROM %s WHERE id=?" % table, (row_id,))
    if not r or r["mode"] != mode_arg():
        raise engine.GameError("Not found")


@app.before_request
def require_login():
    p = request.path
    if p.startswith("/api/") and not p.startswith(PUBLIC_API) and not current_user():
        return jsonify({"error": "Please log in", "login": True}), 401
    if p == "/" and not current_user():
        return redirect("/login")
    return None


def body() -> dict:
    return request.get_json(silent=True) or {}


@app.errorhandler(engine.GameError)
def game_error(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(Exception)
def any_error(e):
    if hasattr(e, "code") and isinstance(getattr(e, "code"), int) and e.code < 500:
        return jsonify({"error": str(e)}), e.code
    log.error("unhandled: %s\n%s", e, traceback.format_exc())
    return jsonify({"error": "Something went wrong: %s" % e}), 500


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    return render_template("index.html", port=config.PORT)


@app.route("/login")
def login_page():
    if current_user():
        return redirect("/")
    return render_template("login.html")


# ------------------------------------------------------------------ accounts
@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    b = body()
    try:
        u = auth.signup(b.get("username", ""), b.get("password", ""), client_ip())
    except auth.AuthError as e:
        return jsonify({"error": str(e)}), 400
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    engine.account(u["owner"])
    return jsonify({"ok": True, "user": {"username": u["username"]}})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    b = body()
    try:
        u = auth.login(b.get("username", ""), b.get("password", ""), client_ip())
    except auth.AuthError as e:
        return jsonify({"error": str(e)}), 400
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    return jsonify({"ok": True, "user": {"username": u["username"]}})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify({"user": None})
    return jsonify({"user": {"username": u["username"], "is_admin": u["is_admin"], "created_ts": u["created_ts"]}})


@app.route("/api/auth/password", methods=["POST"])
def api_password():
    u = current_user()
    if not u:
        return jsonify({"error": "Please log in"}), 401
    b = body()
    try:
        auth.change_password(u["id"], b.get("old", ""), b.get("new", ""))
    except auth.AuthError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/leaderboard")
def api_leaderboard():
    return jsonify(auth.leaderboard(current_user()["id"]))


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "app": "Ticker Tycoon", "port": config.PORT, **fetchers.status(),
                    "market": markethours.status(time.time())})


# ------------------------------------------------------------------ state & market data
@app.route("/api/state")
def api_state():
    mode = mode_arg()
    try:
        engine.settle(mode)
    except Exception as e:
        log.warning("settle failed: %s", e)
    return jsonify(engine.state(mode, hello=request.args.get("hello") == "1"))


@app.route("/api/search")
def api_search():
    watch = {r["asset_id"] for r in db.q("SELECT asset_id FROM watchlist WHERE owner=?", (mode_arg(),))}
    filt = request.args.get("filter", "all")
    if filt == "movers":
        mv = fetchers.get_movers()
        res = []
        for m in mv:
            a = fetchers.asset(m["id"])
            if a:
                res.append(dict(fetchers.public(a), change_pct=m["change_pct"]))
        q = (request.args.get("q") or "").lower()
        if q:
            res = [r for r in res if q in r["symbol"].lower() or q in r["name"].lower()]
        return jsonify({"results": res, "watch": list(watch)})
    res = fetchers.search(request.args.get("q", ""), filt, request.args.get("sector", ""),
                          int(request.args.get("limit", 40)), watch)
    return jsonify({"results": res, "watch": list(watch)})


@app.route("/api/quotes")
def api_quotes():
    ids = [i for i in (request.args.get("ids") or "").split(",") if i][:60]
    mode = mode_arg()
    if mode == "turbo":
        # only tickers that already exist in the simulator; others show their real price
        out = {}
        real = []
        for i in ids:
            if sim.state(i):
                q = sim.quote(i)
                if q:
                    out[i] = q
                    continue
            real.append(i)
        out.update(fetchers.get_quotes(real) if real else {})
        return jsonify(out)
    return jsonify(fetchers.get_quotes(ids))


@app.route("/api/asset/<path:aid>")
def api_asset(aid):
    a = fetchers.asset(aid)
    if not a:
        return jsonify({"error": "Unknown ticker"}), 404
    mode = mode_arg()
    q = (fetchers.get_quotes([aid], max_age=config.LIVE_QUOTE_TTL) if mode == "live" else engine.quotes(mode, [aid])).get(aid)
    info = fetchers.public(a)
    if a["kind"] == "stock" and not a.get("sector"):
        try:
            info["sector"] = fetchers.get_sector(aid)
        except Exception:
            pass
    watch = bool(db.q1("SELECT 1 FROM watchlist WHERE owner=? AND asset_id=?", (mode, aid)))
    st = fetchers.get_stats(aid) if request.args.get("stats") == "1" else None
    return jsonify({"asset": info, "quote": q, "watch": watch, "stats": st,
                    "market_open": engine.market_open(mode, aid), "market": markethours.status(time.time())})


@app.route("/api/candles")
def api_candles():
    aid = request.args.get("id", "")
    tf = request.args.get("tf", "1D")
    mode = mode_arg()
    if not fetchers.asset(aid):
        return jsonify({"error": "Unknown ticker"}), 404
    if mode == "turbo":
        c = sim.candles(aid, tf)
        note = None if c else "No price data yet for this ticker (needs one real price to start the simulation)."
    else:
        c = fetchers.get_candles(aid, tf)
        note = None if c else "No chart data available right now (data source unreachable or ticker has no history)."
    since = request.args.get("since")
    if since:
        try:
            s = int(float(since))
            c = [x for x in c if x["time"] >= s]
        except ValueError:
            pass
    return jsonify({"candles": c, "note": note, "tf": tf})


@app.route("/api/news")
def api_news():
    aid = request.args.get("id") or None
    return jsonify(fetchers.get_news(aid))


@app.route("/api/watchlist", methods=["POST"])
def api_watch():
    b = body()
    aid = b.get("id", "")
    if not fetchers.asset(aid):
        return jsonify({"error": "Unknown ticker"}), 404
    o = mode_arg()
    if db.q1("SELECT 1 FROM watchlist WHERE owner=? AND asset_id=?", (o, aid)):
        db.x("DELETE FROM watchlist WHERE owner=? AND asset_id=?", (o, aid))
        return jsonify({"watch": False})
    db.x("INSERT INTO watchlist(owner, asset_id, added_ts) VALUES(?,?,?)", (o, aid, time.time()))
    return jsonify({"watch": True})


@app.route("/api/sectors")
def api_sectors():
    import seed
    return jsonify({"sectors": seed.SECTORS})


# ------------------------------------------------------------------ trading
@app.route("/api/order", methods=["POST"])
def api_order():
    b = body()
    return jsonify(engine.place_order(mode_arg(), b.get("id", ""), b.get("side", ""), b.get("amount"), b.get("qty"),
                                      b.get("leverage", 1), b.get("sl"), b.get("tp")))


@app.route("/api/position/<int:pid>/close", methods=["POST"])
def api_close(pid):
    own("positions", pid)
    return jsonify(engine.close_position(pid, float(body().get("fraction", 1.0))))


@app.route("/api/position/<int:pid>/stops", methods=["POST"])
def api_stops(pid):
    own("positions", pid)
    b = body()
    return jsonify(engine.set_stops(pid, b.get("sl"), b.get("tp")))


@app.route("/api/position/<int:pid>/margin", methods=["POST"])
def api_margin(pid):
    own("positions", pid)
    return jsonify(engine.add_margin(pid, body().get("amount", 0)))


@app.route("/api/order/<int:oid>/cancel", methods=["POST"])
def api_cancel(oid):
    own("orders", oid)
    return jsonify(engine.cancel_order(oid))


@app.route("/api/redeem", methods=["POST"])
def api_redeem():
    return jsonify(engine.redeem(mode_arg(), body().get("code", "")))


# ------------------------------------------------------------------ bots & funds
@app.route("/api/bots")
def api_bots():
    return jsonify(engine.bots_view(mode_arg()))


@app.route("/api/bots", methods=["POST"])
def api_bot_create():
    b = body()
    return jsonify(engine.create_bot(mode_arg(), b.get("kind", ""), b.get("tickers", []), b.get("budget", 0), b.get("params")))


@app.route("/api/bots/<int:bid>/toggle", methods=["POST"])
def api_bot_toggle(bid):
    own("bots", bid)
    return jsonify(engine.toggle_bot(bid))


@app.route("/api/bots/<int:bid>/sell", methods=["POST"])
def api_bot_sell(bid):
    own("bots", bid)
    return jsonify(engine.sell_bot(bid))


@app.route("/api/funds")
def api_funds():
    return jsonify(engine.funds_view(mode_arg()))


@app.route("/api/funds", methods=["POST"])
def api_fund_create():
    b = body()
    res = engine.create_fund(mode_arg(), b.get("name", ""), b.get("holdings", []))
    if b.get("invest"):
        engine.invest_fund(res["fund_id"], b["invest"])
    return jsonify(res)


@app.route("/api/funds/<int:fid>/invest", methods=["POST"])
def api_fund_invest(fid):
    own("funds", fid)
    return jsonify(engine.invest_fund(fid, body().get("amount", 0)))


@app.route("/api/funds/<int:fid>/withdraw", methods=["POST"])
def api_fund_withdraw(fid):
    own("funds", fid)
    return jsonify(engine.withdraw_fund(fid, body().get("fraction", 1.0)))


# ------------------------------------------------------------------ stats / learn / settings
@app.route("/api/stats")
def api_stats():
    return jsonify(engine.stats(mode_arg()))


@app.route("/api/events")
def api_events():
    return jsonify(db.q("SELECT * FROM events WHERE mode=? ORDER BY ts DESC LIMIT 60", (mode_arg(),)))


@app.route("/api/learn")
def api_learn():
    return jsonify(learn.lessons_view(mode_arg()))


@app.route("/api/learn/videos")
def api_learn_videos():
    return jsonify(learn.videos(request.args.get("lesson", "")))


@app.route("/api/learn/search")
def api_learn_search():
    return jsonify(learn.search_videos(request.args.get("q", "")))


@app.route("/api/learn/complete", methods=["POST"])
def api_learn_complete():
    return jsonify(learn.complete(body().get("lesson", ""), mode_arg()))


@app.route("/api/learn/quiz", methods=["POST"])
def api_learn_quiz():
    b = body()
    return jsonify(learn.quiz(b.get("lesson", ""), b.get("answers", []), mode_arg()))


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    owner = mode_arg()
    u = current_user()
    if request.method == "POST":
        b = body()
        if "market_hours" in b:  # per player: follow real NYSE hours, or let stocks trade any time
            engine.set_setting(owner, "market_hours", bool(b["market_hours"]))
            engine._last_settle_run[owner] = 0
            engine.settle(owner, force=True)   # queued stock orders fill right away when hours are switched off
        if b.get("refresh_symbols"):
            if not u["is_admin"]:
                raise engine.GameError("Only the admin can refresh the symbol lists")
            threading.Thread(target=fetchers.load_symbols, kwargs={"force": True}, daemon=True).start()
    st = fetchers.status()
    if not u["is_admin"]:
        st.pop("sources", None)
    return jsonify({"market_hours": engine.get_setting(owner, "market_hours", True), "is_admin": u["is_admin"],
                    "username": u["username"], **st})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    b = body()
    if b.get("confirm") != "RESET":
        raise engine.GameError("Type RESET to confirm")
    engine.reset(mode_arg())
    return jsonify({"ok": True})


# ------------------------------------------------------------------ dev helpers (TT_DEBUG=1 only)
@app.route("/api/dev/timewarp", methods=["POST"])
def api_timewarp():
    """Pretend the game was closed for N hours (for testing offline settlement)."""
    if not config.DEBUG:
        return jsonify({"error": "debug only"}), 403
    b = body()
    hours = float(b.get("hours", 2))
    mode = mode_arg()
    sec = hours * 3600
    with db.tx():
        db.x("UPDATE accounts SET last_settle_ts=last_settle_ts-?, last_seen_ts=last_seen_ts-? WHERE mode=?", (sec, sec, mode))
        db.x("UPDATE positions SET opened_ts=opened_ts-? WHERE mode=?", (sec, mode))
        db.x("UPDATE networth SET ts=ts-? WHERE mode=?", (sec, mode))
        db.x("UPDATE events SET ts=ts-? WHERE mode=?", (sec, mode))
        if mode == "turbo":
            a = db.kv_get("turbo_anchor")
            a["real"] -= sec
            db.kv_set("turbo_anchor", a)
    engine._last_settle_run[mode] = 0
    return jsonify({"ok": True, "hours": hours})


@app.route("/api/dev/xp", methods=["POST"])
def api_dev_xp():
    if not config.DEBUG:
        return jsonify({"error": "debug only"}), 403
    return jsonify(engine.add_xp(int(body().get("xp", 0)), "dev", mode_arg()))


# ------------------------------------------------------------------ startup
def startup() -> None:
    global _started
    if _started:
        return
    _started = True
    db.conn()
    app.secret_key = config.SECRET_KEY or auth.secret_key()
    fetchers._load_memory()

    def warm():
        fetchers.load_yfinance()
        info = fetchers.load_symbols()
        log.info("symbols ready: %s", info)
        for m in engine.active_owners():
            try:
                engine.settle(m, force=True)
            except Exception as e:
                log.warning("initial settle %s failed: %s", m, e)
    threading.Thread(target=warm, daemon=True).start()
    threading.Thread(target=engine.background_loop, args=(STOP,), daemon=True).start()


if __name__ == "__main__":
    startup()
    print("\n  Ticker Tycoon running at http://localhost:%d  (save file: %s)\n" % (config.PORT, config.DB_PATH))
    try:  # production-grade server when available (many players at once)
        from waitress import serve
        serve(app, host=config.HOST, port=config.PORT, threads=24, ident="TickerTycoon")
    except ImportError:
        app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True, use_reloader=False)
