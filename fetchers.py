"""Real market data: symbol lists, quotes, candles, dividends, news, movers and YouTube search.

Sources
  * Stocks/ETFs list ...... NASDAQ Trader symbol directory (nasdaqlisted.txt + otherlisted.txt), daily
  * Crypto list ........... CoinGecko /coins/list + /coins/markets, daily
  * Stock quotes/history .. yfinance (Yahoo), direct Yahoo chart API fallback, Finnhub quote fallback
  * Crypto quotes ......... CoinGecko /simple/price; crypto candles from Yahoo (validated) or CoinGecko
  * News .................. Finnhub (if FINNHUB_KEY) else Yahoo news
  * Videos ................ YouTube Data API v3 (if YOUTUBE_API_KEY)
Every function degrades gracefully: on failure it serves stale cache and records the error in STATUS.
"""
from __future__ import annotations

import difflib
import io
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

import config
import db
import seed

log = logging.getLogger("tt.fetch")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "*/*"})

STATUS: Dict[str, dict] = {k: {"ok": None, "error": None, "ts": None}
                           for k in ("nasdaq", "coingecko", "yahoo", "finnhub", "youtube")}

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
CG = "https://api.coingecko.com/api/v3"
YH_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
YH_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
FINNHUB = "https://finnhub.io/api/v1"
YT = "https://www.googleapis.com/youtube/v3"

EXCHANGES = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX", "Q": "NASDAQ"}

yf = None          # yfinance (+pandas) is imported lazily in the background so the server starts instantly
HAVE_YF = False   # until it's loaded, the direct Yahoo HTTP fallback is used


def load_yfinance() -> bool:
    """Import yfinance in the background (pandas can take a while on first import)."""
    global yf, HAVE_YF
    if config.FIXTURES or HAVE_YF:
        return HAVE_YF
    try:
        import yfinance as _yf  # type: ignore
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        yf = _yf
        HAVE_YF = True
        log.info("yfinance %s loaded", getattr(_yf, "__version__", "?"))
    except Exception as e:  # pragma: no cover
        log.warning("yfinance unavailable (%s); using direct Yahoo API", e)
    return HAVE_YF


def _mark(src: str, ok: bool, err: str = "") -> None:
    STATUS[src] = {"ok": ok, "error": None if ok else (err or "request failed")[:200], "ts": time.time()}


class FetchError(Exception):
    pass


def http_get(url: str, params: Optional[dict] = None, headers: Optional[dict] = None, timeout: float = 10.0):
    """GET returning a requests.Response-like object. Raises FetchError on network / HTTP errors."""
    if config.FIXTURES:
        from tests import fixtures  # noqa
        return fixtures.fake_http(url, params or {})
    try:
        r = _session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise FetchError(str(e))
    if r.status_code == 429:
        raise FetchError("rate limited (429)")
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    return r


# --------------------------------------------------------------------------- rate limiting
class _Limiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.last = 0.0
        self.blocked_until = 0.0

    def wait(self) -> bool:
        with self.lock:
            now = time.time()
            if now < self.blocked_until:
                return False
            d = self.last + self.min_interval - now
            if d > 0:
                time.sleep(min(d, 5))
            self.last = time.time()
            return True

    def backoff(self, seconds: float) -> None:
        self.blocked_until = time.time() + seconds


CG_LIMIT = _Limiter(1.2 if config.COINGECKO_KEY else 2.5)
FH_LIMIT = _Limiter(1.1)


def cg_get(path: str, params: Optional[dict] = None, timeout: float = 15.0):
    if not config.FIXTURES and not CG_LIMIT.wait():
        raise FetchError("CoinGecko cooling down after rate limit")
    headers = {"x-cg-demo-api-key": config.COINGECKO_KEY} if config.COINGECKO_KEY else None
    try:
        r = http_get(CG + path, params, headers, timeout)
        _mark("coingecko", True)
        return r.json()
    except FetchError as e:
        if "429" in str(e):
            CG_LIMIT.backoff(60)
        _mark("coingecko", False, str(e))
        raise


# --------------------------------------------------------------------------- symbol universe
SYMS: List[dict] = []
BY_ID: Dict[str, dict] = {}
_sym_lock = threading.Lock()
_POP_RANK = {s: i for i, s in enumerate(seed.POPULAR_ORDER)}


def stock_yahoo(sym: str) -> str:
    return sym.replace(".", "-").replace("$", "-P").replace("/", "-")


def _parse_pipe_file(text: str) -> List[dict]:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    header = lines[0].split("|")
    out = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) != len(header):
            continue
        out.append(dict(zip(header, parts)))
    return out


def parse_nasdaq(text: str) -> List[dict]:
    rows = []
    for r in _parse_pipe_file(text):
        if r.get("Test Issue", "N") == "Y":
            continue
        sym = r.get("Symbol", "").strip()
        if not sym:
            continue
        etf = r.get("ETF", "N") == "Y"
        rows.append({"id": "S:" + sym, "kind": "etf" if etf else "stock", "symbol": sym,
                     "name": _clean_name(r.get("Security Name", "")), "exchange": "NASDAQ",
                     "rank": None, "logo": None, "sector": "ETF" if etf else None, "yahoo": stock_yahoo(sym)})
    return rows


def parse_other(text: str) -> List[dict]:
    rows = []
    for r in _parse_pipe_file(text):
        if r.get("Test Issue", "N") == "Y":
            continue
        sym = r.get("ACT Symbol", "").strip()
        if not sym:
            continue
        etf = r.get("ETF", "N") == "Y"
        rows.append({"id": "S:" + sym, "kind": "etf" if etf else "stock", "symbol": sym,
                     "name": _clean_name(r.get("Security Name", "")),
                     "exchange": EXCHANGES.get(r.get("Exchange", ""), "US exchange"),
                     "rank": None, "logo": None, "sector": "ETF" if etf else None, "yahoo": stock_yahoo(sym)})
    return rows


def _clean_name(n: str) -> str:
    n = n.strip()
    for suf in (" - Common Stock", " Common Stock", " - Class A Common Stock", " - American Depositary Shares"):
        if n.endswith(suf) and len(n) > len(suf) + 2:
            return n[: -len(suf)].strip()
    return n


def _seed_rows() -> List[dict]:
    rows = []
    for sym, (name, sector, kind) in seed.POPULAR_STOCKS.items():
        rows.append({"id": "S:" + sym, "kind": kind, "symbol": sym, "name": name, "exchange": "",
                     "rank": None, "logo": None, "sector": sector, "yahoo": stock_yahoo(sym)})
    for cid, sym, name, rank in seed.POPULAR_COINS:
        rows.append({"id": "C:" + cid, "kind": "crypto", "symbol": sym, "name": name, "exchange": "Crypto",
                     "rank": rank, "logo": None, "sector": "Crypto", "yahoo": sym.upper() + "-USD"})
    return rows


def _save_rows(rows: List[dict], kinds: tuple) -> None:
    keep = {r["id"]: r for r in db.q("SELECT id, sector, logo FROM symbols")}
    for r in rows:
        old = keep.get(r["id"])
        if old:
            r["sector"] = r["sector"] or old["sector"]
            r["logo"] = r["logo"] or old["logo"]
    with db.tx():
        db.conn().execute("DELETE FROM symbols WHERE kind IN (%s)" % ",".join("?" * len(kinds)), kinds)
        db.conn().executemany(
            "INSERT OR REPLACE INTO symbols(id,kind,symbol,name,exchange,rank,logo,sector,yahoo) VALUES(?,?,?,?,?,?,?,?,?)",
            [(r["id"], r["kind"], r["symbol"], r["name"], r["exchange"], r["rank"], r["logo"], r["sector"], r["yahoo"])
             for r in rows])


def refresh_stocks() -> int:
    try:
        a = http_get(NASDAQ_URL, timeout=30).text
        b = http_get(OTHER_URL, timeout=30).text
        rows = parse_nasdaq(a)
        seen = {r["id"] for r in rows}
        rows += [r for r in parse_other(b) if r["id"] not in seen]
        if len(rows) < 1000:
            raise FetchError("symbol file looked truncated")
        for r in rows:  # seed sectors
            s = seed.POPULAR_STOCKS.get(r["symbol"])
            if s and not r["sector"]:
                r["sector"] = s[1]
        _save_rows(rows, ("stock", "etf"))
        db.kv_set("stocks_ts", time.time())
        _mark("nasdaq", True)
        return len(rows)
    except Exception as e:
        _mark("nasdaq", False, str(e))
        log.warning("stock list refresh failed: %s", e)
        return 0


def refresh_coins() -> int:
    try:
        lst = cg_get("/coins/list", timeout=30)
        markets = {}
        for page in (1, 2):  # top 500 by market cap (keeps us under CoinGecko's free rate limit)
            try:
                data = cg_get("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc",
                                                  "per_page": 250, "page": page, "price_change_percentage": "24h"})
            except FetchError:
                break
            for m in data:
                markets[m["id"]] = m
        rows = []
        now = time.time()
        prices = []
        for c in lst:
            cid = c.get("id")
            if not cid:
                continue
            m = markets.get(cid, {})
            rows.append({"id": "C:" + cid, "kind": "crypto", "symbol": (c.get("symbol") or "").lower(),
                         "name": c.get("name") or cid, "exchange": "Crypto", "rank": m.get("market_cap_rank"),
                         "logo": m.get("image"), "sector": "Crypto", "yahoo": (c.get("symbol") or "").upper() + "-USD"})
            if m.get("current_price") is not None:
                chg = m.get("price_change_percentage_24h") or 0.0
                p = float(m["current_price"])
                prices.append(("C:" + cid, p, p / (1 + chg / 100.0) if chg > -100 else p, chg, now))
        if len(rows) < 500:
            raise FetchError("coin list looked truncated")
        _save_rows(rows, ("crypto",))
        db.xmany("INSERT OR REPLACE INTO last_price(id,price,prev_close,change_pct,ts) VALUES(?,?,?,?,?)", prices)
        db.cache_set("cg_markets", [{"id": "C:" + k, "chg": v.get("price_change_percentage_24h") or 0,
                                      "mcap": v.get("market_cap") or 0} for k, v in markets.items()], now)
        db.kv_set("coins_ts", now)
        return len(rows)
    except Exception as e:
        log.warning("coin list refresh failed: %s", e)
        return 0


def load_symbols(force: bool = False) -> dict:
    """Load symbol universe into memory, downloading fresh lists if older than a day."""
    now = time.time()
    n_st = n_co = 0
    if force or now - (db.kv_get("stocks_ts", 0) or 0) > config.SYMBOL_LIST_TTL:
        n_st = refresh_stocks()
    if force or now - (db.kv_get("coins_ts", 0) or 0) > config.SYMBOL_LIST_TTL:
        n_co = refresh_coins()
    if (db.q1("SELECT COUNT(*) n FROM symbols") or {"n": 0})["n"] == 0:
        rows = _seed_rows()
        _save_rows([r for r in rows if r["kind"] != "crypto"], ("stock", "etf"))
        _save_rows([r for r in rows if r["kind"] == "crypto"], ("crypto",))
    _load_memory()
    return {"stocks_downloaded": n_st, "coins_downloaded": n_co, "total": len(SYMS)}


def _load_memory() -> None:
    rows = db.q("SELECT * FROM symbols")
    for r in rows:
        r["_s"] = (r["symbol"] or "").lower()
        r["_n"] = (r["name"] or "").lower()
        r["_w"] = re.split(r"[\s\-\.,&/()]+", r["_n"])
        if r["kind"] != "crypto":
            sd = seed.POPULAR_STOCKS.get(r["symbol"])
            if sd and not r["sector"]:
                r["sector"] = sd[1]
    with _sym_lock:
        SYMS[:] = rows
        BY_ID.clear()
        BY_ID.update({r["id"]: r for r in rows})


def asset(aid: str) -> Optional[dict]:
    a = BY_ID.get(aid)
    if a:
        return a
    r = db.q1("SELECT * FROM symbols WHERE id=?", (aid,))
    return r


def public(a: dict) -> dict:
    return {k: a.get(k) for k in ("id", "kind", "symbol", "name", "exchange", "rank", "logo", "sector")}


_BAD_NAME = re.compile(r"warrant|\bright|\bunits?\b|depositary shares? represent|preferred|notes due|%", re.I)


def _score(r: dict, q: str) -> float:
    s, n = r["_s"], r["_n"]
    sc = 0.0
    if s == q:
        sc = 1000
    elif n == q:
        sc = 950
    elif s.startswith(q):
        sc = 800 - 10 * (len(s) - len(q))
    elif n.startswith(q):
        sc = 700
    elif any(w.startswith(q) for w in r["_w"]):
        sc = 600
    elif q in n:
        sc = 420
    elif len(q) >= 2 and q in s:
        sc = 380
    else:
        return 0
    return sc + _bonus(r)


def _bonus(r: dict) -> float:
    b = 0.0
    if r["kind"] == "crypto":
        rk = r.get("rank")
        b += (250 - min(rk, 250)) * 1.2 + 60 if rk else -250
    else:
        if r["symbol"] in _POP_RANK:
            b += 220 - _POP_RANK[r["symbol"]] * 3
        elif r["symbol"] in seed.POPULAR_STOCKS:
            b += 120
        if _BAD_NAME.search(r["name"] or ""):
            b -= 200
        if r["exchange"] in ("NASDAQ", "NYSE"):
            b += 20
    return b


def _fuzzy(q: str, pool: List[dict], limit: int) -> List[tuple]:
    """Typo-tolerant pass (e.g. 'nvdia', 'etherium')."""
    out = []
    first = q[0]
    for r in pool:
        cand = r["_w"][0] if r["_w"] else r["_n"]
        if not cand or (cand[0] != first and r["_s"][:1] != first):
            continue
        ratio = max(difflib.SequenceMatcher(None, q, cand).ratio(),
                    difflib.SequenceMatcher(None, q, r["_s"]).ratio() if len(q) <= 6 else 0)
        if ratio >= 0.72:
            out.append((ratio * 300 + _bonus(r), r))
    out.sort(key=lambda t: -t[0])
    return out[:limit]


def search(q: str, filt: str = "all", sector: str = "", limit: int = 40, watch: Optional[set] = None) -> List[dict]:
    q = (q or "").strip().lower().lstrip("$")
    with _sym_lock:
        pool = SYMS
        if filt == "stocks":
            pool = [r for r in pool if r["kind"] == "stock"]
        elif filt == "etfs":
            pool = [r for r in pool if r["kind"] == "etf"]
        elif filt == "crypto":
            pool = [r for r in pool if r["kind"] == "crypto"]
        elif filt == "sector":
            pool = [r for r in pool if r["kind"] == "stock" and r.get("sector") and (not sector or r["sector"] == sector)]
        elif filt == "watchlist":
            pool = [r for r in pool if watch and r["id"] in watch]
        if not q:
            if filt == "watchlist":
                return [public(r) for r in pool][:limit]
            ranked = sorted(pool, key=lambda r: -_bonus(r))
            return [public(r) for r in ranked[:limit]]
        scored = []
        for r in pool:
            sc = _score(r, q)
            if sc:
                scored.append((sc, r))
        scored.sort(key=lambda t: -t[0])
        res = scored[:limit]
        if len(res) < 8 and len(q) >= 3:
            have = {r["id"] for _, r in res}
            res += [t for t in _fuzzy(q, pool, limit) if t[1]["id"] not in have]
        return [public(r) for _, r in res[:limit]]


# --------------------------------------------------------------------------- quotes
_qcache: Dict[str, dict] = {}
_qlock = threading.Lock()


def _store_prices(rows: Dict[str, dict]) -> None:
    now = time.time()
    with _qlock:
        for k, v in rows.items():
            v["ts"] = now
            _qcache[k] = v
    db.xmany("INSERT OR REPLACE INTO last_price(id,price,prev_close,change_pct,ts) VALUES(?,?,?,?,?)",
             [(k, v["price"], v.get("prev_close"), v.get("change_pct"), now) for k, v in rows.items()])


def _yahoo_chart(ysym: str, rng: str, interval: str, events: bool = False) -> dict:
    params = {"range": rng, "interval": interval, "includePrePost": "false"}
    if events:
        params["events"] = "div"
    r = http_get(YH_CHART + ysym, params)
    js = r.json()
    res = (js.get("chart") or {}).get("result") or []
    if not res:
        raise FetchError("no chart data for %s" % ysym)
    return res[0]


def _yahoo_quote_direct(ysym: str) -> Optional[dict]:
    res = _yahoo_chart(ysym, "5d", "1d")
    meta = res.get("meta", {})
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    closes = [c for c in (((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c]
    if len(closes) >= 2:
        prev = closes[-2]
    if price is None:
        return None
    prev = prev or price
    return {"price": float(price), "prev_close": float(prev), "change_pct": (price / prev - 1) * 100 if prev else 0.0}


def _yf_batch_quotes(ysyms: List[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not HAVE_YF or not ysyms:
        return out
    df = yf_call(lambda: yf.download(ysyms, period="5d", interval="1d", group_by="ticker", progress=False,
                                     threads=True, auto_adjust=False, timeout=10), 15)
    if df is None or df.empty:
        return out
    for ys in ysyms:
        try:
            sub = df[ys] if ys in df.columns.get_level_values(0) else df
            closes = sub["Close"].dropna()
            if closes.empty:
                continue
            price = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) > 1 else price
            if math.isnan(price):
                continue
            out[ys] = {"price": price, "prev_close": prev, "change_pct": (price / prev - 1) * 100 if prev else 0}
        except Exception:
            continue
    return out


def _finnhub_quote(sym: str) -> Optional[dict]:
    if not config.FINNHUB_KEY:
        return None
    FH_LIMIT.wait()
    try:
        js = http_get(FINNHUB + "/quote", {"symbol": sym, "token": config.FINNHUB_KEY}).json()
        _mark("finnhub", True)
        if js.get("c"):
            pc = js.get("pc") or js["c"]
            return {"price": float(js["c"]), "prev_close": float(pc), "change_pct": float(js.get("dp") or 0)}
    except Exception as e:
        _mark("finnhub", False, str(e))
    return None


def _direct_quotes(ymap: Dict[str, str]) -> Dict[str, dict]:
    """aid -> yahoo symbol; fetches latest quotes in parallel straight from Yahoo's chart API."""
    out: Dict[str, dict] = {}
    if not ymap:
        return out

    def one(aid):
        try:
            v = _yahoo_quote_direct(ymap[aid])
            return aid, (dict(v, source="yahoo") if v else None)
        except Exception as e:
            return aid, e
    with ThreadPoolExecutor(max_workers=8) as ex:
        for aid, v in ex.map(one, list(ymap)):
            if isinstance(v, dict):
                out[aid] = v
            elif isinstance(v, Exception):
                _mark("yahoo", False, str(v))
    if out:
        _mark("yahoo", True)
    return out


def _fetch_stock_quotes(aids: List[str]) -> Dict[str, dict]:
    m = {}
    for aid in aids:
        a = asset(aid)
        m[aid] = (a or {}).get("yahoo") or stock_yahoo(aid[2:])
    out = _direct_quotes(m)
    missing = [aid for aid in aids if aid not in out]
    if missing and HAVE_YF:
        got = _yf_batch_quotes([m[a] for a in missing])
        for aid in missing:
            if m[aid] in got:
                out[aid] = dict(got[m[aid]], source="yfinance")
    for aid in [a for a in aids if a not in out]:
        v = _finnhub_quote(aid[2:])
        if v:
            out[aid] = dict(v, source="finnhub")
    return out


def _yahoo_crypto(aids: List[str], out: Dict[str, dict]) -> None:
    """Yahoo's XYZ-USD quote for well-known coins, checked against CoinGecko's last price (same ticker can be a different coin)."""
    ymap = {}
    for aid in aids:
        if aid in out:
            continue
        a = asset(aid) or {}
        if (a.get("rank") or 9999) <= 400 and db.kv_get("yh_ok:" + aid) is not False:
            ymap[aid] = a.get("yahoo") or aid[2:].upper() + "-USD"
    if not ymap:
        return
    for aid, v in _direct_quotes(ymap).items():
        ref = db.q1("SELECT price FROM last_price WHERE id=?", (aid,))
        if ref and ref["price"]:
            if abs(v["price"] / ref["price"] - 1) > 0.15:
                db.kv_set("yh_ok:" + aid, False)
                continue
        elif ((asset(aid) or {}).get("rank") or 9999) > 50:
            continue
        out[aid] = v


def _fetch_crypto_quotes(aids: List[str], prefer_yahoo: bool = False) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if prefer_yahoo:  # fast polling of the coin you're looking at: Yahoo has no tight rate limit
        _yahoo_crypto(aids, out)
    ids = [a[2:] for a in aids if a not in out]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        try:
            js = cg_get("/simple/price", {"ids": ",".join(chunk), "vs_currencies": "usd", "include_24hr_change": "true"})
        except FetchError:
            break
        for cid, v in js.items():
            if "usd" not in v:
                continue
            p = float(v["usd"])
            chg = float(v.get("usd_24h_change") or 0)
            out["C:" + cid] = {"price": p, "prev_close": p / (1 + chg / 100) if chg > -100 else p,
                               "change_pct": chg, "source": "coingecko"}
    # CoinGecko busy (free-tier rate limit)? fall back to Yahoo for well-known coins
    _yahoo_crypto(aids, out)
    return out


def get_quotes(aids: List[str], max_age: Optional[float] = None) -> Dict[str, dict]:
    """Batch quotes with TTL cache (stocks 60s, crypto 30s). Stale values are returned with stale=True."""
    now = time.time()
    aids = [a for a in dict.fromkeys(aids) if a and a[:2] in ("S:", "C:")][:300]
    res: Dict[str, dict] = {}
    need_s, need_c = [], []
    with _qlock:
        for a in aids:
            ttl = max_age if max_age is not None else (config.CRYPTO_QUOTE_TTL if a.startswith("C:") else config.STOCK_QUOTE_TTL)
            c = _qcache.get(a)
            if c and now - c["ts"] < ttl:
                res[a] = dict(c, stale=False)
            elif a.startswith("C:"):
                need_c.append(a)
            else:
                need_s.append(a)
    fresh: Dict[str, dict] = {}
    if need_s:
        fresh.update(_fetch_stock_quotes(need_s))
    if need_c:
        fresh.update(_fetch_crypto_quotes(need_c, prefer_yahoo=max_age is not None and max_age < config.CRYPTO_QUOTE_TTL))
    if fresh:
        _store_prices(fresh)
        for k, v in fresh.items():
            res[k] = dict(v, stale=False)
    missing = [a for a in aids if a not in res]
    if missing:
        rows = db.q("SELECT * FROM last_price WHERE id IN (%s)" % ",".join("?" * len(missing)), tuple(missing))
        for r in rows:
            res[r["id"]] = {"price": r["price"], "prev_close": r["prev_close"], "change_pct": r["change_pct"],
                            "ts": r["ts"], "stale": True, "source": "cache"}
    return res


def get_price(aid: str) -> Optional[float]:
    q = get_quotes([aid]).get(aid)
    return q["price"] if q else None


# --------------------------------------------------------------------------- candles
TF = {  # timeframe -> (yahoo range, yahoo interval, seconds per bar)
    "1m": ("5d", "1m", 60),
    "5m": ("1mo", "5m", 300),
    "1h": ("6mo", "1h", 3600),
    "1D": ("5y", "1d", 86400),
    "1W": ("max", "1wk", 604800),
}
_ccache: Dict[str, tuple] = {}


def _chart_to_candles(res: dict) -> List[dict]:
    ts = res.get("timestamp") or []
    qd = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c, v = (qd.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    out = []
    for i, t in enumerate(ts):
        try:
            if c[i] is None or o[i] is None:
                continue
            out.append({"time": int(t), "open": float(o[i]), "high": float(h[i]), "low": float(l[i]),
                        "close": float(c[i]), "volume": float(v[i] or 0) if i < len(v) else 0.0})
        except (IndexError, TypeError):
            continue
    return out


_YF_POOL = ThreadPoolExecutor(max_workers=4)


def yf_call(fn, timeout: float = 12.0):
    """Run a yfinance call with a hard time limit so a stuck request can never freeze the game."""
    try:
        return _YF_POOL.submit(fn).result(timeout=timeout)
    except Exception:
        return None


def _yf_history(ysym: str, rng: str, interval: str) -> List[dict]:
    if not HAVE_YF:
        return []
    df = yf_call(lambda: yf.Ticker(ysym).history(period=rng, interval=interval, auto_adjust=False, actions=False))
    if df is None or df.empty:
        return []
    out = []
    for idx, row in df.iterrows():
        try:
            if any(math.isnan(float(row[k])) for k in ("Open", "High", "Low", "Close")):
                continue
            out.append({"time": int(idx.timestamp()), "open": float(row["Open"]), "high": float(row["High"]),
                        "low": float(row["Low"]), "close": float(row["Close"]),
                        "volume": float(row.get("Volume", 0) or 0)})
        except Exception:
            continue
    return out


def _yahoo_candles(ysym: str, rng: str, interval: str) -> List[dict]:
    """Direct Yahoo chart API first (fast, one request), yfinance as the fallback."""
    try:
        c = _chart_to_candles(_yahoo_chart(ysym, rng, interval))
        if c:
            _mark("yahoo", True)
            return c
    except Exception as e:
        _mark("yahoo", False, str(e))
    c = _yf_history(ysym, rng, interval)
    if c:
        _mark("yahoo", True)
    return c


def _dedupe(c: List[dict]) -> List[dict]:
    seen = {}
    for x in c:
        seen[x["time"]] = x
    return [seen[k] for k in sorted(seen)]


def _cg_candles(cid: str, tf: str) -> List[dict]:
    days = {"1m": 1, "5m": 1, "1h": 30, "1D": 365, "1W": 365}[tf]
    js = cg_get("/coins/%s/market_chart" % cid, {"vs_currency": "usd", "days": days})
    prices = js.get("prices") or []
    vols = {int(t / 1000): v for t, v in (js.get("total_volumes") or [])}
    step = {"1m": 300, "5m": 300, "1h": 3600, "1D": 86400, "1W": 604800}[tf]
    buckets: Dict[int, dict] = {}
    for t, p in prices:
        t = int(t / 1000)
        b = t - t % step
        k = buckets.get(b)
        if not k:
            buckets[b] = {"time": b, "open": p, "high": p, "low": p, "close": p, "volume": 0.0}
        else:
            k["high"] = max(k["high"], p)
            k["low"] = min(k["low"], p)
            k["close"] = p
    n = max(1, len(buckets))
    for b, k in buckets.items():
        k["volume"] = (vols.get(b) or 0) / max(1, (86400 / step)) if vols else 0
    return [buckets[k] for k in sorted(buckets)][-1500:] if n else []


def _crypto_yahoo_ok(aid: str, candles: List[dict]) -> bool:
    """Yahoo's XYZ-USD may be a different coin than CoinGecko's - verify the price matches."""
    if not candles:
        return False
    with _qlock:
        q = _qcache.get(aid)
    ref = q["price"] if q else None
    if ref is None:
        r = db.q1("SELECT price FROM last_price WHERE id=?", (aid,))
        ref = r["price"] if r else None
    if not ref:
        return False
    return abs(candles[-1]["close"] / ref - 1) < 0.06


def get_candles(aid: str, tf: str) -> List[dict]:
    tf = tf if tf in TF else "1D"
    key = aid + "|" + tf
    now = time.time()
    c = _ccache.get(key)
    if c and now - c[0] < config.HISTORY_TTL[tf]:
        return c[1]
    a = asset(aid) or {}
    rng, interval, _ = TF[tf]
    candles: List[dict] = []
    if aid.startswith("S:"):
        candles = _yahoo_candles(a.get("yahoo") or stock_yahoo(aid[2:]), rng, interval)
    else:
        use_yh = db.kv_get("yh_ok:" + aid)
        if use_yh is not False and (a.get("rank") or 9999) <= 400:
            candles = _yahoo_candles(a.get("yahoo") or aid[2:].upper() + "-USD", rng, interval)
            ok = _crypto_yahoo_ok(aid, candles)
            if candles:
                db.kv_set("yh_ok:" + aid, ok)
            if not ok:
                candles = []
        if not candles:
            try:
                candles = _cg_candles(aid[2:], tf)
            except Exception:
                candles = []
    candles = _dedupe(candles)
    if candles:
        _ccache[key] = (now, candles)
        db.cache_set("candles:" + key, candles[-600:], now)
    else:
        stale = db.cache_get_stale("candles:" + key)
        candles = stale or []
    return candles


def _aggregate(candles: List[dict], step: int) -> List[dict]:
    out: Dict[int, dict] = {}
    for x in candles:
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


def get_candles_between(aid: str, start: float, end: float) -> List[dict]:
    """Real candles covering (start, end] for offline settlement. Picks the finest resolution available."""
    gap = end - start
    if gap <= 6 * 86400:
        tf = "1m" if gap <= 2 * 86400 else "5m"
    elif gap <= 55 * 86400:
        tf = "5m"
    else:
        tf = "1h"
    a = asset(aid) or {}
    candles: List[dict] = []
    if aid.startswith("S:") or db.kv_get("yh_ok:" + aid) is not False:
        rng, interval, _ = TF[tf]
        if tf == "5m":
            rng = "60d" if gap > 20 * 86400 else "1mo"
        if tf == "1h":
            rng = "2y"
        ysym = a.get("yahoo") or (stock_yahoo(aid[2:]) if aid.startswith("S:") else aid[2:].upper() + "-USD")
        candles = _yahoo_candles(ysym, rng, interval)
        if aid.startswith("C:") and not _crypto_yahoo_ok(aid, candles):
            candles = []
    if not candles and aid.startswith("C:"):
        try:
            js = cg_get("/coins/%s/market_chart/range" % aid[2:],
                        {"vs_currency": "usd", "from": int(start) - 600, "to": int(end)})
            candles = [{"time": int(t / 1000), "open": p, "high": p, "low": p, "close": p, "volume": 0}
                       for t, p in js.get("prices") or []]
        except Exception:
            candles = []
    return [x for x in _dedupe(candles) if start < x["time"] <= end]


# --------------------------------------------------------------------------- volatility & dividends
def get_stats(aid: str) -> dict:
    """Annualised volatility, average volume and trailing dividend yield from real daily history."""
    now = time.time()
    ck = "stats:" + aid
    c = db.cache_get(ck, 86400, now)
    if c:
        return c
    daily = get_candles(aid, "1D")[-260:]
    closes = [x["close"] for x in daily if x["close"] > 0]
    ann = 365 if aid.startswith("C:") else 252
    sigma = None
    if len(closes) > 20:
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
        sigma = sd * math.sqrt(ann)
    if not sigma or sigma != sigma:
        sigma = 0.65 if aid.startswith("C:") else 0.32
    sigma = min(max(sigma, 0.05), 2.5)
    vols = [x["volume"] for x in daily[-30:] if x["volume"]]
    avg_vol = sum(vols) / len(vols) if vols else 1_000_000
    divy = 0.0
    if aid.startswith("S:") and closes:
        year_ago = now - 365 * 86400
        tot = sum(d["amount"] for d in get_dividends(aid) if d["ts"] >= year_ago)
        divy = tot / closes[-1] if closes[-1] else 0.0
    out = {"sigma": sigma, "avg_vol": avg_vol, "div_yield": divy, "real": bool(len(closes) > 20)}
    if out["real"]:
        db.cache_set(ck, out, now)
    return out


def get_dividends(aid: str) -> List[dict]:
    """Real dividend history: [{'ts': ex-date unix, 'amount': $ per share}] (last ~2 years)."""
    if not aid.startswith("S:"):
        return []
    now = time.time()
    ck = "divs:" + aid
    c = db.cache_get(ck, 12 * 3600, now)
    if c is not None:
        return c
    a = asset(aid) or {}
    ys = a.get("yahoo") or stock_yahoo(aid[2:])
    out: List[dict] = []
    got = False
    if HAVE_YF:
        try:
            s = yf_call(lambda: yf.Ticker(ys).dividends)
            if s is None:
                raise FetchError('timeout')
            for idx, amt in s.items():
                out.append({"ts": int(idx.timestamp()), "amount": float(amt)})
            got = True
        except Exception:
            pass
    if not got:
        try:
            res = _yahoo_chart(ys, "2y", "1d", events=True)
            for _, d in ((res.get("events") or {}).get("dividends") or {}).items():
                out.append({"ts": int(d["date"]), "amount": float(d["amount"])})
            got = True
        except Exception:
            pass
    out = sorted([d for d in out if d["ts"] > now - 800 * 86400], key=lambda d: d["ts"])
    if got:
        db.cache_set(ck, out, now)
    return out


# --------------------------------------------------------------------------- news
def _fmt_news(title, source, url, ts, summary="", image=None):
    return {"title": title, "source": source or "", "url": url, "ts": ts, "summary": (summary or "")[:280],
            "image": image}


def _yahoo_news_items(items: list) -> List[dict]:
    out = []
    for it in items or []:
        c = it.get("content") if isinstance(it.get("content"), dict) else None
        if c:  # yfinance >= 0.2.48 format
            url = ((c.get("clickThroughUrl") or {}) or {}).get("url") or ((c.get("canonicalUrl") or {}) or {}).get("url")
            ts = None
            if c.get("pubDate"):
                try:
                    ts = datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = None
            img = None
            try:
                img = (c.get("thumbnail") or {}).get("resolutions", [{}])[0].get("url")
            except Exception:
                pass
            if c.get("title") and url:
                out.append(_fmt_news(c["title"], (c.get("provider") or {}).get("displayName"), url, ts,
                                     c.get("summary"), img))
        elif it.get("title") and it.get("link"):
            img = None
            try:
                img = (it.get("thumbnail") or {}).get("resolutions", [{}])[0].get("url")
            except Exception:
                pass
            out.append(_fmt_news(it["title"], it.get("publisher"), it["link"], it.get("providerPublishTime"), "", img))
    return out


def get_news(aid: Optional[str] = None) -> dict:
    now = time.time()
    ck = "news:" + (aid or "market")
    c = db.cache_get(ck, config.NEWS_TTL, now)
    if c:
        return c
    a = asset(aid) if aid else None
    items: List[dict] = []
    source = ""
    note = None
    if config.FINNHUB_KEY:
        try:
            FH_LIMIT.wait()
            if aid and aid.startswith("S:"):
                frm = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
                to = datetime.utcnow().strftime("%Y-%m-%d")
                js = http_get(FINNHUB + "/company-news", {"symbol": aid[2:], "from": frm, "to": to,
                                                           "token": config.FINNHUB_KEY}).json()
            else:
                js = http_get(FINNHUB + "/news", {"category": "crypto" if aid else "general",
                                                   "token": config.FINNHUB_KEY}).json()
                if a:
                    keys = {a["symbol"].lower(), a["name"].lower()}
                    rel = [n for n in js if any(k in (n.get("headline", "") + " " + n.get("related", "")).lower() for k in keys)]
                    js = rel or js
            items = [_fmt_news(n.get("headline"), n.get("source"), n.get("url"), n.get("datetime"),
                               n.get("summary"), n.get("image") or None) for n in js[:25] if n.get("headline")]
            source = "Finnhub"
            _mark("finnhub", True)
        except Exception as e:
            _mark("finnhub", False, str(e))
    else:
        note = "Add FINNHUB_KEY to .env for richer company & crypto news (using Yahoo news meanwhile)."
    if not items:
        ys = (a or {}).get("yahoo") if a else None
        if a and a["kind"] == "crypto":
            ys = a["symbol"].upper() + "-USD"
        if HAVE_YF and ys:
            try:
                items = _yahoo_news_items(yf_call(lambda: yf.Ticker(ys).news) or [])
                source = "Yahoo Finance"
            except Exception:
                items = []
        if not items:
            try:
                qs = ys or "stock market"
                js = http_get(YH_SEARCH, {"q": qs, "newsCount": 15, "quotesCount": 0}).json()
                items = _yahoo_news_items(js.get("news") or [])
                source = "Yahoo Finance"
                _mark("yahoo", True)
            except Exception as e:
                _mark("yahoo", False, str(e))
    items = [i for i in items if i.get("title")]
    items.sort(key=lambda i: -(i.get("ts") or 0))
    out = {"items": items[:20], "source": source, "note": note}
    if items:
        db.cache_set(ck, out, now)
    else:
        stale = db.cache_get_stale(ck)
        if stale:
            return stale
    return out


# --------------------------------------------------------------------------- movers & sectors
def get_movers(limit: int = 30) -> List[dict]:
    now = time.time()
    c = db.cache_get("movers", 300, now)
    if c:
        return c
    out: List[dict] = []
    # stocks: Yahoo predefined screeners
    for scr in ("day_gainers", "day_losers"):
        quotes = []
        if HAVE_YF:
            try:
                quotes = (yf_call(lambda: yf.screen(scr, count=15)) or {}).get("quotes", [])
            except Exception:
                quotes = []
        if not quotes:
            try:
                js = http_get("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                              {"scrIds": scr, "count": 15}).json()
                quotes = (((js.get("finance") or {}).get("result") or [{}])[0]).get("quotes", [])
            except Exception:
                quotes = []
        for qd in quotes:
            sym = (qd.get("symbol") or "").replace("-", ".")
            aid = "S:" + sym
            if aid in BY_ID or asset(aid):
                out.append({"id": aid, "change_pct": qd.get("regularMarketChangePercent") or 0})
    # crypto: from CoinGecko markets snapshot (top coins by market cap)
    mk = db.cache_get_stale("cg_markets") or []
    mk = sorted(mk, key=lambda m: -(m.get("mcap") or 0))[:250]
    mk.sort(key=lambda m: -abs(m.get("chg") or 0))
    out += [{"id": m["id"], "change_pct": m["chg"]} for m in mk[:20]]
    if not any(o["id"].startswith("S:") for o in out):  # fallback: popular stocks by cached change
        rows = db.q("SELECT id, change_pct FROM last_price WHERE id LIKE 'S:%' AND change_pct IS NOT NULL")
        rows.sort(key=lambda r: -abs(r["change_pct"] or 0))
        out += [{"id": r["id"], "change_pct": r["change_pct"]} for r in rows[:15]]
    seen = set()
    out = [m for m in sorted(out, key=lambda m: -abs(m["change_pct"] or 0)) if not (m["id"] in seen or seen.add(m["id"]))]
    out = out[:limit]
    db.cache_set("movers", out, now)
    return out


def get_sector(aid: str) -> Optional[str]:
    a = asset(aid)
    if not a or a["kind"] != "stock":
        return (a or {}).get("sector")
    if a.get("sector"):
        return a["sector"]
    sector = None
    if HAVE_YF:
        try:
            sector = (yf_call(lambda: yf.Ticker(a["yahoo"]).info, 8) or {}).get("sector")
        except Exception:
            sector = None
    if not sector and config.FINNHUB_KEY:
        try:
            js = http_get(FINNHUB + "/stock/profile2", {"symbol": a["symbol"], "token": config.FINNHUB_KEY}).json()
            sector = js.get("finnhubIndustry")
            if js.get("logo"):
                db.x("UPDATE symbols SET logo=? WHERE id=?", (js["logo"], aid))
                a["logo"] = js["logo"]
        except Exception:
            pass
    if sector:
        db.x("UPDATE symbols SET sector=? WHERE id=?", (sector, aid))
        a["sector"] = sector
    return sector


# --------------------------------------------------------------------------- YouTube
def _iso_dur(s: str) -> int:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def youtube_search(query: str, n: int = 4) -> Optional[List[dict]]:
    """Find the best educational videos for a query. None if no API key; [] on error."""
    if not config.YOUTUBE_API_KEY:
        return None
    now = time.time()
    ck = "yt:" + query.lower().strip()
    c = db.cache_get(ck, config.YOUTUBE_CACHE_TTL, now)
    if c is not None:
        return c[:n]
    try:
        s = http_get(YT + "/search", {"part": "snippet", "q": query, "type": "video", "maxResults": 25,
                                      "relevanceLanguage": "en", "safeSearch": "strict", "videoEmbeddable": "true",
                                      "key": config.YOUTUBE_API_KEY}).json()
        ids = [it["id"]["videoId"] for it in s.get("items", []) if it.get("id", {}).get("videoId")]
        if not ids:
            db.cache_set(ck, [], now)
            return []
        v = http_get(YT + "/videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids),
                                      "key": config.YOUTUBE_API_KEY}).json()
        _mark("youtube", True)
    except Exception as e:
        _mark("youtube", False, str(e))
        stale = db.cache_get_stale(ck)
        return (stale or [])[:n]
    vids = []
    for it in v.get("items", []):
        st = it.get("statistics", {})
        sn = it.get("snippet", {})
        views = int(st.get("viewCount", 0) or 0)
        likes = int(st.get("likeCount", 0) or 0)
        dur = _iso_dur(it.get("contentDetails", {}).get("duration", ""))
        if dur < 90 or dur > 60 * 60:  # skip Shorts and multi-hour streams
            continue
        try:
            pub = datetime.fromisoformat(sn.get("publishedAt", "").replace("Z", "+00:00")).timestamp()
        except Exception:
            pub = now - 5 * 365 * 86400
        age_y = max(0.0, (now - pub) / (365 * 86400))
        like_ratio = likes / views if views else 0
        score = (0.45 * min(1.0, math.log10(views + 1) / 7.0)
                 + 0.35 * min(1.0, like_ratio / 0.04)
                 + 0.20 * math.exp(-age_y / 4.0))
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
        vids.append({"id": it["id"], "title": sn.get("title"), "channel": sn.get("channelTitle"), "views": views,
                     "likes": likes, "like_ratio": round(like_ratio, 4), "published": sn.get("publishedAt"),
                     "duration": dur, "thumb": thumb, "score": round(score, 4),
                     "url": "https://www.youtube.com/watch?v=" + it["id"]})
    vids.sort(key=lambda x: -x["score"])
    vids = vids[:8]
    db.cache_set(ck, vids, now)
    return vids[:n]


def status() -> dict:
    return {"sources": STATUS, "yfinance": HAVE_YF,
            "keys": {"finnhub": bool(config.FINNHUB_KEY), "youtube": bool(config.YOUTUBE_API_KEY),
                     "coingecko": bool(config.COINGECKO_KEY)},
            "symbols": {"stocks": sum(1 for r in SYMS if r["kind"] == "stock"),
                        "etfs": sum(1 for r in SYMS if r["kind"] == "etf"),
                        "crypto": sum(1 for r in SYMS if r["kind"] == "crypto"),
                        "stocks_ts": db.kv_get("stocks_ts"), "coins_ts": db.kv_get("coins_ts")}}
