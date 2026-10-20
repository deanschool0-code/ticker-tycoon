"""Offline stand-ins that mimic the REAL response formats of NASDAQ Trader, CoinGecko, Yahoo and YouTube.
Only used by the test-suite (TT_FIXTURES=1) so the game logic can be tested without network access."""
from __future__ import annotations

import json
import math
import random
import time
from urllib.parse import urlparse


class Resp:
    def __init__(self, data=None, text=None, status=200):
        self._data = data
        self.text = text if text is not None else json.dumps(data)
        self.status_code = status

    def json(self):
        return self._data if self._data is not None else json.loads(self.text)


BASE = {"AAPL": 230.0, "NVDA": 180.0, "TSLA": 250.0, "SPY": 600.0, "KO": 70.0, "AMD": 150.0, "QQQ": 520.0,
        "BTC-USD": 100000.0, "ETH-USD": 3500.0, "DOGE-USD": 0.20, "AEYE": 6.5, "BRK-A": 700000.0}
CG_PRICE = {"bitcoin": 100000.0, "ethereum": 3500.0, "dogecoin": 0.20, "bitcoin-fake": 0.0001}
DIP = {"DOGE-USD": (3600, 0.17)}  # dip to 0.17 about one hour ago (for the stop-loss test)


def nasdaq_txt() -> str:
    rows = ["Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"]
    rows += ["AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N",
             "NVDA|NVIDIA Corporation - Common Stock|Q|N|N|100|N|N",
             "TSLA|Tesla, Inc. - Common Stock|Q|N|N|100|N|N",
             "AMD|Advanced Micro Devices, Inc. - Common Stock|Q|N|N|100|N|N",
             "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N",
             "AEYE|AudioEye, Inc. - Common Stock|S|N|N|100|N|N",
             "ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N"]
    for i in range(1200):
        rows.append("F%03d|Filler Corp %d - Common Stock|S|N|N|100|N|N" % (i, i))
    rows.append("File Creation Time: 1002202617:01|||||||")
    return "\n".join(rows)


def other_txt() -> str:
    rows = ["ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol",
            "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY",
            "KO|Coca-Cola Company (The) Common Stock|N|KO|N|100|N|KO",
            "BRK.A|Berkshire Hathaway Inc.|N|BRK.A|N|1|N|BRK=A",
            "NTEST|NYSE TEST ISSUE|N|NTEST|N|100|Y|NTEST",
            "File Creation Time: 1002202617:01|||||||"]
    return "\n".join(rows)


def coins_list():
    out = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}, {"id": "ethereum", "symbol": "eth", "name": "Ethereum"},
           {"id": "dogecoin", "symbol": "doge", "name": "Dogecoin"},
           {"id": "bitcoin-fake", "symbol": "btc", "name": "Bitcoin Fake Token"}]
    out += [{"id": "coin-%d" % i, "symbol": "c%d" % i, "name": "Coin %d" % i} for i in range(700)]
    return out


def markets(page):
    if str(page) != "1":
        return []
    return [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "image": "https://example.invalid/btc.png",
             "current_price": CG_PRICE["bitcoin"], "market_cap": 2e12, "market_cap_rank": 1, "price_change_percentage_24h": 1.5},
            {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "image": None, "current_price": CG_PRICE["ethereum"],
             "market_cap": 4e11, "market_cap_rank": 2, "price_change_percentage_24h": -2.0},
            {"id": "dogecoin", "symbol": "doge", "name": "Dogecoin", "image": None, "current_price": CG_PRICE["dogecoin"],
             "market_cap": 3e10, "market_cap_rank": 8, "price_change_percentage_24h": 6.0}]


INTERVAL = {"1m": 60, "5m": 300, "1h": 3600, "60m": 3600, "1d": 86400, "1wk": 604800}
RANGE = {"1d": 1, "5d": 5, "1mo": 30, "60d": 60, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825, "max": 3650}


def chart(sym, rng, interval, events=False):
    base = BASE.get(sym)
    if base is None:
        return {"chart": {"result": None, "error": {"code": "Not Found"}}}
    step = INTERVAL.get(interval, 86400)
    days = RANGE.get(rng, 5)
    n = min(int(days * 86400 / step), 3000)
    now = int(time.time())
    end = now - now % step
    rnd = random.Random(sym + interval)
    ts, o, h, l, c, v = [], [], [], [], [], []
    p = base
    closes = []
    for i in range(n):
        closes.append(p)
        p *= math.exp(rnd.gauss(0, 0.002 if step < 3600 else 0.015))
    shift = base / closes[-1]
    closes = [x * shift for x in closes]
    for i in range(n):
        t = end - (n - 1 - i) * step
        cl = closes[i]
        op = closes[i - 1] if i else cl
        hi, lo = max(op, cl) * 1.001, min(op, cl) * 0.999
        if sym in DIP and step == 60:
            ago, low = DIP[sym]
            if abs((now - t) - ago) < 300:
                lo = low
            op = cl = base
            hi = base * 1.001
            lo = lo if abs((now - t) - ago) < 300 else base * 0.999
        ts.append(t); o.append(op); h.append(hi); l.append(lo); c.append(cl); v.append(1000 + i)
    res = {"meta": {"symbol": sym, "regularMarketPrice": base, "chartPreviousClose": base * 0.99},
           "timestamp": ts, "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}
    if events and sym == "KO":
        exd = now - 3600
        res["events"] = {"dividends": {str(exd): {"amount": 0.51, "date": exd},
                                       str(exd - 91 * 86400): {"amount": 0.51, "date": exd - 91 * 86400}}}
    return {"chart": {"result": [res], "error": None}}


def youtube_search(q):
    return {"items": [{"id": {"kind": "youtube#video", "videoId": "vid%02d" % i}} for i in range(6)]}


def youtube_videos(ids):
    items = []
    for i, vid in enumerate(ids.split(",")):
        items.append({"id": vid, "snippet": {"title": "Lesson video %d" % i, "channelTitle": "Edu Channel %d" % i,
                                             "publishedAt": "202%d-05-01T10:00:00Z" % (i % 6),
                                             "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/%s/mqdefault.jpg" % vid}}},
                      "statistics": {"viewCount": str(10 ** (3 + i)), "likeCount": str(int(10 ** (3 + i) * 0.03))},
                      "contentDetails": {"duration": "PT%dM%dS" % (3 + i, 10) if i != 2 else "PT45S"}})
    return {"items": items}


def fake_http(url, params):
    u = urlparse(url)
    path = u.path
    if "nasdaqlisted" in path:
        return Resp(text=nasdaq_txt())
    if "otherlisted" in path:
        return Resp(text=other_txt())
    if "coingecko" in u.netloc:
        if path.endswith("/coins/list"):
            return Resp(coins_list())
        if path.endswith("/coins/markets"):
            return Resp(markets(params.get("page", 1)))
        if path.endswith("/simple/price"):
            out = {}
            for cid in params.get("ids", "").split(","):
                if cid in CG_PRICE:
                    out[cid] = {"usd": CG_PRICE[cid], "usd_24h_change": 1.0}
            return Resp(out)
        if "/market_chart" in path:
            cid = path.split("/coins/")[1].split("/")[0]
            p = CG_PRICE.get(cid, 1.0)
            now = int(time.time() * 1000)
            return Resp({"prices": [[now - i * 300000, p] for i in range(288)][::-1],
                         "total_volumes": [[now - i * 300000, 1e6] for i in range(288)][::-1]})
    if "finance.yahoo.com" in u.netloc:
        if "/v8/finance/chart/" in path:
            sym = path.rsplit("/", 1)[1]
            return Resp(chart(sym, params.get("range", "5d"), params.get("interval", "1d"), params.get("events") == "div"))
        if "/v1/finance/search" in path:
            return Resp({"news": [{"title": "%s moves as markets react" % params.get("q"), "publisher": "Reuters",
                                   "link": "https://example.invalid/news/1", "providerPublishTime": int(time.time()) - 600}]})
        if "screener" in path:
            return Resp({"finance": {"result": [{"quotes": [{"symbol": "AEYE", "regularMarketChangePercent": 18.5}]}]}})
    if "googleapis.com" in u.netloc:
        if path.endswith("/search"):
            return Resp(youtube_search(params.get("q")))
        if path.endswith("/videos"):
            return Resp(youtube_videos(params.get("id", "")))
    from fetchers import FetchError
    raise FetchError("fixture: no route for %s" % url)
