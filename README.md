# Ticker Tycoon

An idle stock & crypto trading game with **real** stocks, **real** coins and **real** prices.
Virtual money only. Educational, not financial advice.

```
cd ~/Desktop/TickerTycoon && bash run.sh      ->  http://localhost:5558
```

`run.sh` creates a Python environment in `~/Library/Application Support/TickerTycoon/venv` (outside iCloud-synced folders), installs `requirements.txt`, copies `.env.example` to `.env` (first run),
starts the server and opens your browser. Run it again any time; if the game is already running it just opens it.
Stop it with **Ctrl+C**. Your progress is saved in `game.db` in `~/Library/Application Support/TickerTycoon/` (kept out of iCloud so syncing can't corrupt it).

## What's inside

| File | What it does |
|---|---|
| `app.py` | Flask server and JSON API (port 5558) |
| `fetchers.py` | Real data: NASDAQ Trader symbol lists, CoinGecko, yfinance/Yahoo, Finnhub, YouTube |
| `engine.py` | Game engine: orders, stops, leverage/margin, XP, bots, funds, dividends, offline settlement |
| `auth.py` | Player accounts, logins, leaderboard |
| `sim.py` | Price simulator from an earlier Turbo mode (not used in the game anymore) |
| `markethours.py` | Real NYSE hours, holidays and early closes (2025-2027) |
| `learn.py` | Learn tab lessons, quizzes, practice setups, video search |
| `templates/`, `static/` | Front-end (vanilla JS + CSS, TradingView lightweight-charts from unpkg CDN) |
| `tests/test_e2e.py` | End-to-end test with offline fixtures: `python -m tests.test_e2e` |

## Players & accounts
Everyone has their own account (username + password) with their own $10,000, positions, bots, funds, XP, lessons and watchlist.
Log in from any computer to continue. The **first account created is the admin** (sees API-key/data-source status in Settings).
There's a leaderboard in the Stats tab. Each player can switch real market hours off in Settings to trade stocks any time.

## Run it as the public server (schoolmarket.win)
On the server Mac (the one running the Cloudflare tunnel), with this folder on it:

```
cd ~/Desktop/TickerTycoon && bash server-setup.sh
```

It stops SchoolMarket (its background service is moved to `~/Library/LaunchAgents/disabled`, nothing is deleted),
installs the game into `~/TickerTycoon-server`, runs it in the background on port 5558 (auto-starts at login, restarts if it crashes),
points `schoolmarket.win` at it in `~/.cloudflared/config.yml` (a backup of the old config is saved next to it) and checks the site.
Run the same command again to update the server after changes; accounts and progress are kept in
`~/Library/Application Support/TickerTycoon/game.db`.

## Data sources
* **Stocks & ETFs**: full lists from `nasdaqlisted.txt` + `otherlisted.txt` (NASDAQ, NYSE, NYSE American, NYSE Arca, Cboe, ETFs), test issues removed. Cached in `game.db`, refreshed daily.
* **Crypto**: CoinGecko `/coins/list` (~20k coins) + `/coins/markets` (top 1,000 with logos, market cap, 24h change). Refreshed daily.
* **Prices**: yfinance for stock quotes/history (no key), Finnhub as optional fallback, CoinGecko for crypto. Only tickers you can see or hold are fetched, in batches, cached ~60s (stocks) / ~30s (crypto).
* **News**: Finnhub company & crypto news if you add a key, otherwise Yahoo Finance news.

## API keys (all optional, all free)
Open `.env` in this folder, paste keys, then restart with `bash run.sh`. Settings tab shows which keys are active.

**Finnhub (`FINNHUB_KEY`)**: better news, quote fallback, company logos
1. Go to https://finnhub.io/register and create a free account.
2. Your API key is on the dashboard right after you log in. Copy it into `FINNHUB_KEY=`.

**YouTube Data API v3 (`YOUTUBE_API_KEY`)**: Learn tab finds, ranks and embeds videos
1. Go to https://console.cloud.google.com and sign in with your Google account.
2. Create a project (top bar > project picker > New project), name it "Ticker Tycoon".
3. Menu > APIs & Services > Library > search **YouTube Data API v3** > Enable.
4. APIs & Services > Credentials > **Create credentials > API key**. Copy it into `YOUTUBE_API_KEY=`.
5. Optional: "Restrict key" > API restrictions > YouTube Data API v3.
The free quota is 10,000 units/day; each new search costs ~101 units and results are cached for 7 days, so normal play uses very little.
Without a key, each lesson shows hand-picked links to well-known educational channels (Khan Academy, The Plain Bagel, Investopedia, Rayner Teo...).

**CoinGecko (`COINGECKO_KEY`)**: optional "Demo" key for higher crypto rate limits: https://www.coingecko.com/en/api/pricing

## How the game works
* **LIVE mode**: real prices in real time. Stocks follow real NYSE hours; orders placed while closed are queued and fill at the open. Crypto trades 24/7. While the server runs, stops/bots/dividends keep working; when you reopen after the server was off, the game replays the real candles you missed and shows **While you were away**.
* Start: $10,000 + a free $1,000 SPY starter position. Codes in the News panel (N): `WELCOME` (+$5,000), `TYCOON`, `LEARNER`.
* Long/short, $ or shares, 25/50/75/100% sizing, stop loss & take profit (type, click the chart, or drag the lines), partial closes.
* Nothing is locked: 1x-10x leverage, bots and funds are available from the start; XP raises your level and rank. Margin call warning at 50% of margin lost, liquidation at 90%.
* Real dividends are paid into cash on real ex-dividend dates (shorts pay them).
* Bots: DCA, MA crossover, RSI dip buyer, momentum, grid (crypto). Funds: your own ETF-style baskets.

**Keyboard**: B long, S short, C close, E edit stops, N news/codes, / search, 1-7 tabs, ? help, Esc back.

## Troubleshooting
* **"Chart library could not load"**: the chart comes from unpkg.com, so you need internet.
* **Port in use**: `lsof -i :5558` then quit the other process, or set `PORT=5560` in `.env`.
* **Start fresh**: Settings > Reset (per mode), or quit and delete `~/Library/Application Support/TickerTycoon/game.db`.
* **Stuck after "Ticker Tycoon -> http://localhost:5558"**: the page loads within a few seconds now; real data (symbol lists, yfinance) keeps loading in the background.
* **Testing offline settlement**: start with `TT_DEBUG=1 bash run.sh`, then `curl -X POST localhost:5558/api/dev/timewarp -H 'Content-Type: application/json' -d '{"mode":"live","hours":2}'` and reload the page.
