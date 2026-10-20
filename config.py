"""Ticker Tycoon configuration: paths, keys (.env) and game constants."""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:  # .env is optional
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover
    pass


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v.lower() in ("", "your_key_here", "changeme", "xxx"):
        return ""
    return v


PORT = int(os.environ.get("PORT", "5558"))
HOST = os.environ.get("HOST", "127.0.0.1")   # the Cloudflare tunnel connects locally, so no need to expose the port
SECRET_KEY = _env("SECRET_KEY")              # signs login cookies; auto-generated and saved in game.db if empty
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0") == "1"
def _default_db() -> Path:
    """game.db lives outside iCloud-synced folders on a Mac (syncing a live SQLite file can corrupt it)."""
    local = BASE_DIR / "game.db"
    if local.exists():
        return local
    home = Path.home()
    if sys.platform == "darwin":
        d = home / "Library" / "Application Support" / "TickerTycoon"
        d.mkdir(parents=True, exist_ok=True)
        return d / "game.db"
    return local


DB_PATH = Path(os.environ["TT_DB"]) if os.environ.get("TT_DB") else _default_db()
DEBUG = os.environ.get("TT_DEBUG", "0") == "1"          # enables /api/dev/* helpers
FIXTURES = os.environ.get("TT_FIXTURES", "0") == "1"    # offline test data (tests only)

FINNHUB_KEY = _env("FINNHUB_KEY")
YOUTUBE_API_KEY = _env("YOUTUBE_API_KEY")
COINGECKO_KEY = _env("COINGECKO_KEY")  # optional free "demo" key, raises rate limits

# ---- caching ----
STOCK_QUOTE_TTL = 60          # seconds
CRYPTO_QUOTE_TTL = 30
SYMBOL_LIST_TTL = 24 * 3600   # refresh symbol lists daily
YOUTUBE_CACHE_TTL = 7 * 24 * 3600
NEWS_TTL = 15 * 60
HISTORY_TTL = {"1m": 10, "5m": 20, "1h": 120, "1D": 600, "1W": 3600}
LIVE_QUOTE_TTL = 5            # the ticker you are looking at refreshes every ~5s

# ---- game ----
START_CASH = 10_000.0
STARTER_POSITION_USD = 1_000.0          # free starter position
STARTER_SYMBOL = "S:SPY"
CODES = {
    "WELCOME": {"cash": 5_000.0, "label": "+$5,000 welcome bonus"},
    "TYCOON": {"cash": 2_500.0, "label": "+$2,500 tycoon starter pack"},
    "LEARNER": {"xp": 150, "label": "+150 XP study bonus"},
}
TURBO_SPEEDS = [60, 600, 3600]          # simulated seconds per real second
DEFAULT_TURBO_SPEED = 600
MARGIN_CALL_RATIO = 0.5    # loss >= 50% of a position's margin -> warning
LIQUIDATION_RATIO = 0.9    # loss >= 90% of margin -> liquidated
AWAY_THRESHOLD = 10 * 60   # show "while you were away" after 10 minutes

# Nothing is locked: every feature is available from the start. Levels are just for bragging rights.
# xp needed for level L = 50 * L * (L - 1)
UNLOCKS = []
RANKS = [(1, "Rookie"), (3, "Apprentice Trader"), (5, "Trader"), (8, "Pro Trader"), (12, "Market Maker"),
         (16, "Hedge Fund Manager"), (20, "Wall Street Legend"), (25, "Ticker Tycoon")]

BOT_TYPES = {
    "dca": {"name": "DCA Bot", "price": 2_000, "desc": "Buys a fixed $ amount of each ticker on a schedule.", "crypto_only": False},
    "ma_cross": {"name": "MA Crossover Bot", "price": 4_000, "desc": "Buys when the 9-bar average crosses above the 21-bar average, sells on the cross down.", "crypto_only": False},
    "rsi_dip": {"name": "RSI Dip Buyer", "price": 6_000, "desc": "Buys when RSI(14) drops below 30 (oversold) and sells above 70.", "crypto_only": False},
    "momentum": {"name": "Momentum Bot", "price": 8_000, "desc": "Rides strong moves: buys after a +1.5% run, exits on a 2% trailing drop.", "crypto_only": False},
    "grid": {"name": "Grid Bot", "price": 10_000, "desc": "Crypto only. Places a ladder of buy/sell levels around the start price and harvests the chop.", "crypto_only": True},
}
