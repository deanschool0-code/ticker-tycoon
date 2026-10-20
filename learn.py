"""LEARN tab content: beginner lesson path, quizzes, practice setups, video search and fallbacks."""
from __future__ import annotations

import time
import urllib.parse
from typing import List, Optional

import db
import engine
import fetchers

LESSON_XP = 20
QUIZ_XP = 50

# Hand-picked fallback (used when there is no YOUTUBE_API_KEY): well-known educational channels,
# linked to each channel's own search for the lesson topic.
CHANNELS = {
    "khan": ("Khan Academy", "@khanacademy"),
    "investopedia": ("Investopedia", "@Investopedia"),
    "bagel": ("The Plain Bagel", "@ThePlainBagel"),
    "twocents": ("Two Cents (PBS)", "@TwoCentsPBS"),
    "cnbc": ("CNBC Make It", "@CNBCMakeIt"),
    "wbcrypto": ("Whiteboard Crypto", "@WhiteboardCrypto"),
    "coinbureau": ("Coin Bureau", "@CoinBureau"),
    "felix": ("Ben Felix", "@BenFelixCSI"),
    "rayner": ("Rayner Teo", "@RaynerTeo"),
    "humbled": ("Humbled Trader", "@HumbledTrader"),
}

LESSONS = [
    {
        "id": "stock", "title": "What a stock is", "emoji": "S",
        "query": "what is a stock explained for beginners",
        "fallback": [("khan", "stocks"), ("bagel", "what is a stock"), ("twocents", "stock market")],
        "body": [
            "A stock (also called a share) is a tiny slice of ownership in a real company. Buy one share of Apple and you own a very small piece of Apple.",
            "Companies sell shares to raise money. After that, people trade the shares with each other on exchanges like the NYSE and NASDAQ. The price moves all day because buyers and sellers keep changing what they're willing to pay.",
            "Example with game numbers: you start with $10,000. If AAPL costs $200, $1,000 buys 5 shares. If AAPL rises to $220, your 5 shares are worth $1,100, so you're up $100 (+10%). If it falls to $180, they're worth $900 (-$100).",
            "In Ticker Tycoon, stocks follow real NYSE hours (9:30am-4:00pm New York time, Monday to Friday). Orders placed while the market is closed are queued and fill at the next open.",
        ],
        "practice": {"symbol": "S:AAPL", "side": "long", "amount": 1000, "hint": "Buy $1,000 of Apple and watch it move with the real market."},
        "quiz": [
            {"q": "What do you own when you buy a share?", "options": ["A loan to the company", "A small piece of the company", "A coupon for its products", "A guaranteed profit"], "a": 1,
             "why": "A share is partial ownership of the company."},
            {"q": "You buy 10 shares at $50. The price goes to $55. What's your profit?", "options": ["$5", "$50", "$55", "$500"], "a": 1,
             "why": "10 shares x $5 gain each = $50."},
            {"q": "You place a stock order at 8pm New York time. What happens?", "options": ["It fills instantly", "It's cancelled", "It's queued for the next market open", "It fills at yesterday's price"], "a": 2,
             "why": "Stocks only trade during market hours, so the order waits for the open."},
        ],
    },
    {
        "id": "crypto", "title": "What crypto is", "emoji": "C",
        "query": "what is cryptocurrency explained simply",
        "fallback": [("wbcrypto", "what is cryptocurrency"), ("coinbureau", "crypto for beginners"), ("khan", "bitcoin")],
        "body": [
            "Cryptocurrencies like Bitcoin (BTC) and Ethereum (ETH) are digital money that lives on a blockchain, a shared record that thousands of computers keep in sync. No bank or company owns it.",
            "Unlike stocks, a coin isn't a piece of a company. Its price is purely what people will pay for it, which is why crypto is much more volatile. A 5-10% move in a day is normal; meme coins like DOGE can move far more.",
            "Crypto markets never close: they trade 24 hours a day, 7 days a week, including weekends.",
            "Example: with $1,000 you could buy 0.01 BTC if BTC is $100,000. If BTC drops 10% to $90,000 your 0.01 BTC is worth $900. Same math as stocks, just bigger swings.",
        ],
        "practice": {"symbol": "C:bitcoin", "side": "long", "amount": 500, "hint": "Buy $500 of Bitcoin. Crypto trades 24/7, so this works any time."},
        "quiz": [
            {"q": "When can you trade crypto?", "options": ["Weekdays 9:30-4:00", "Only weekends", "24/7", "Only when banks are open"], "a": 2, "why": "Crypto markets never close."},
            {"q": "Compared to big stocks, crypto prices are usually...", "options": ["Less volatile", "About the same", "More volatile", "Fixed"], "a": 2, "why": "Crypto commonly moves 5-10%+ in a day."},
            {"q": "Owning a coin means you own a piece of a company.", "options": ["True", "False"], "a": 1, "why": "Coins aren't company ownership; stocks are."},
        ],
    },
    {
        "id": "candles", "title": "Reading a candlestick chart", "emoji": "K",
        "query": "how to read candlestick charts for beginners",
        "fallback": [("rayner", "candlestick"), ("humbled", "candlestick patterns"), ("investopedia", "candlestick")],
        "body": [
            "Each candle shows what the price did during one time period (1 minute, 1 hour, 1 day...). It has four prices: Open, High, Low and Close (OHLC).",
            "The thick body runs from open to close. Green body = price closed higher than it opened. Red body = it closed lower. The thin lines (wicks) show the highest and lowest prices reached.",
            "Example: a 1-hour NVDA candle opens at $120, spikes to $124, dips to $119 and closes at $123. That's a green candle with a body from $120 to $123, a top wick to $124 and a bottom wick to $119.",
            "In the Trade tab, switch timeframes (1m, 5m, 1h, 1D, 1W), scroll the mouse wheel to zoom and drag to look back in time. The bars at the bottom are volume: how much traded in each candle.",
        ],
        "practice": {"symbol": "S:NVDA", "tf": "1h", "hint": "Open NVDA on the 1h chart. Zoom with the wheel and find the biggest green candle."},
        "quiz": [
            {"q": "A green candle means...", "options": ["Close was higher than open", "Close was lower than open", "Volume was high", "The market was closed"], "a": 0, "why": "Green = closed above where it opened."},
            {"q": "The thin lines above and below the body are called...", "options": ["Bars", "Wicks", "Gaps", "Lines of credit"], "a": 1, "why": "Wicks show the high and low."},
            {"q": "What does OHLC stand for?", "options": ["Open High Low Close", "Order Hold Long Cash", "Over High Under Low", "Open Hour Last Candle"], "a": 0, "why": "Open, High, Low, Close."},
        ],
    },
    {
        "id": "longshort", "title": "Long vs short", "emoji": "L",
        "query": "long vs short selling explained",
        "fallback": [("bagel", "short selling"), ("investopedia", "short selling"), ("cnbc", "short selling")],
        "body": [
            "Going LONG means you buy hoping the price goes UP. Going SHORT means you bet the price goes DOWN: you sell borrowed shares now and buy them back later, hopefully cheaper.",
            "Long example: buy $1,000 of TSLA at $250 (4 shares). TSLA rises to $275 (+10%): you make +$100.",
            "Short example: short $1,000 of TSLA at $250. TSLA falls to $225 (-10%): you make +$100. But if TSLA rises to $275 you LOSE $100.",
            "The danger of shorting: a stock can only fall to $0, but it can rise without limit. That's why shorts can lose more than you put in, and why Ticker Tycoon can liquidate a short that goes badly wrong. Shorts also pay the dividends instead of receiving them.",
        ],
        "practice": {"symbol": "S:TSLA", "side": "short", "amount": 500, "sl_pct": 5, "hint": "Try a $500 SHORT on Tesla with a 5% stop loss above the price."},
        "quiz": [
            {"q": "You expect a price to fall. Which do you open?", "options": ["Long", "Short"], "a": 1, "why": "Shorts profit when price falls."},
            {"q": "You short $1,000 and the price rises 20%. Your P&L?", "options": ["+$200", "-$200", "$0", "-$1,000"], "a": 1, "why": "Shorts lose when price rises: 20% of $1,000 = -$200."},
            {"q": "Why is shorting riskier than buying?", "options": ["Prices can rise without limit", "Shorts can't be closed", "Shorts never make money", "It's not riskier"], "a": 0, "why": "Loss on a long is capped at 100%; a short's loss isn't."},
        ],
    },
    {
        "id": "stops", "title": "Stop loss & take profit", "emoji": "T",
        "query": "stop loss and take profit explained beginners",
        "fallback": [("rayner", "stop loss"), ("humbled", "stop loss"), ("investopedia", "stop loss order")],
        "body": [
            "A STOP LOSS automatically closes your trade if the price moves against you to a level you choose. A TAKE PROFIT closes it automatically when the price reaches your target.",
            "Example: buy $1,000 of AMD at $150. Set a stop loss at $142.50 (-5%) and a take profit at $165 (+10%). Worst case you lose about $50; best case you lock in $100. That's a 1:2 risk/reward.",
            "Stops work even while Ticker Tycoon is closed: when you come back, the game replays what the real market did and closes anything that hit its stop or target.",
            "In the Trade tab, click 'Set on chart' then click the chart to place a level, or drag the dashed SL/TP lines to move them. Press E to edit stops on your open position.",
        ],
        "practice": {"symbol": "S:AMD", "side": "long", "amount": 1000, "sl_pct": 5, "tp_pct": 10, "hint": "A $1,000 AMD long with a 5% stop loss and 10% take profit is pre-filled. Drag the lines on the chart to adjust."},
        "quiz": [
            {"q": "You buy at $100 with a stop loss at $95. Price drops to $94. What happens?", "options": ["Nothing", "The trade closes near $95", "You buy more", "The stop moves down"], "a": 1, "why": "The stop closes the trade when $95 is reached."},
            {"q": "A take profit is used to...", "options": ["Limit losses", "Lock in gains automatically", "Add leverage", "Pay dividends"], "a": 1, "why": "It closes the trade at your profit target."},
            {"q": "For a SHORT, where does the stop loss go?", "options": ["Below the price", "Above the price", "At zero", "Anywhere"], "a": 1, "why": "A short loses when price rises, so the stop sits above."},
        ],
    },
    {
        "id": "sizing", "title": "Position sizing & risk", "emoji": "R",
        "query": "position sizing risk management trading 1% rule",
        "fallback": [("rayner", "position sizing"), ("humbled", "risk management"), ("bagel", "risk")],
        "body": [
            "Position sizing is deciding HOW MUCH to put in one trade. A simple rule many traders use: never risk more than 1-2% of your account on a single trade.",
            "Example: your account is $10,000. Risking 1% = $100. If your stop loss is 5% away from your entry, your position can be $2,000 (5% of $2,000 = $100). If your stop is 10% away, use $1,000.",
            "Small sizes let you survive a losing streak. Lose 10 trades in a row at 1% risk and you still have about $9,000. Put 50% into every trade and two bad trades can wipe out most of your money.",
            "Use the 25 / 50 / 75 / 100% buttons in the order panel to size quickly, and switch between $ amount and number of shares.",
        ],
        "practice": {"symbol": "S:SPY", "side": "long", "amount": 2000, "sl_pct": 5, "hint": "$2,000 of SPY with a 5% stop risks about $100 (1% of a $10K account)."},
        "quiz": [
            {"q": "Account $10,000, risk 1% per trade. Max loss per trade?", "options": ["$10", "$100", "$1,000", "$10,000"], "a": 1, "why": "1% of $10,000 = $100."},
            {"q": "Risk $100 with a stop 10% below entry. Position size?", "options": ["$100", "$1,000", "$10,000", "$500"], "a": 1, "why": "10% of $1,000 = $100."},
            {"q": "Why size positions small?", "options": ["Bigger profits always", "To survive losing streaks", "Brokers require it", "It avoids taxes"], "a": 1, "why": "Small risk keeps you in the game."},
        ],
    },
    {
        "id": "leverage", "title": "Leverage and why it's dangerous", "emoji": "X",
        "query": "leverage trading explained risks liquidation",
        "fallback": [("bagel", "leverage"), ("coinbureau", "leverage trading"), ("investopedia", "leverage")],
        "body": [
            "Leverage lets you control a bigger position than your cash. With 5x leverage, $1,000 of margin controls a $5,000 position.",
            "It multiplies BOTH directions. On a 5x long, a +10% move earns +$500 (50% on your $1,000). A -10% move loses $500 (half your margin).",
            "If losses eat most of your margin you get a MARGIN CALL warning (the orange bar at the top of the game, at 50% of margin lost). At 90% lost the position is LIQUIDATED: closed automatically and the margin is gone.",
            "On 10x leverage, a move of just 9% against you liquidates the trade. That's why most real brokers cap stock leverage at 2x for beginners: it's the fastest way to blow up an account.",
        ],
        "practice": {"symbol": "C:ethereum", "side": "long", "amount": 1000, "leverage": 2, "sl_pct": 4, "hint": "A 2x leveraged ETH long with a tight stop. Watch the liquidation price in your position card."},
        "quiz": [
            {"q": "$1,000 margin at 5x leverage controls a position of...", "options": ["$200", "$1,000", "$5,000", "$50,000"], "a": 2, "why": "5 x $1,000 = $5,000."},
            {"q": "A 10x long falls 10%. Roughly what happens to your margin?", "options": ["-10%", "-50%", "It's all lost / liquidated", "Nothing"], "a": 2, "why": "10x times -10% = -100% of margin."},
            {"q": "What's a margin call?", "options": ["A bonus", "A warning your losses are eating your margin", "A phone call from your broker about dividends", "A type of stock"], "a": 1, "why": "It warns you before liquidation."},
        ],
    },
    {
        "id": "dividends", "title": "Dividends", "emoji": "D",
        "query": "what are dividends ex-dividend date explained",
        "fallback": [("bagel", "dividends"), ("twocents", "dividends"), ("felix", "dividends")],
        "body": [
            "A dividend is cash a company pays its shareholders, usually every quarter, out of its profits. Coca-Cola, Johnson & Johnson and many ETFs pay them.",
            "What matters is the EX-DIVIDEND DATE: you must own the shares BEFORE that date to get the payment. Buy on or after the ex-date and that dividend goes to the seller.",
            "Example: you own 50 shares of KO. KO pays about $0.53 per share per quarter. On the ex-date Ticker Tycoon pays you 50 x $0.53 = $26.50 in cash, using KO's real dividend data.",
            "Short sellers PAY the dividend instead of receiving it.",
        ],
        "practice": {"symbol": "S:KO", "side": "long", "amount": 2000, "hint": "Coca-Cola is a classic dividend stock. Hold it through an ex-date to get paid."},
        "quiz": [
            {"q": "To receive a dividend you must own the stock before...", "options": ["The pay date", "The ex-dividend date", "The earnings call", "January 1"], "a": 1, "why": "Ownership before the ex-date is what counts."},
            {"q": "100 shares, $0.25 dividend. You receive...", "options": ["$0.25", "$2.50", "$25", "$250"], "a": 2, "why": "100 x $0.25 = $25."},
            {"q": "If you're SHORT on the ex-date you...", "options": ["Receive the dividend", "Pay the dividend", "Nothing happens", "Get double"], "a": 1, "why": "Shorts owe the dividend to the lender."},
        ],
    },
    {
        "id": "etfs", "title": "Diversification & ETFs", "emoji": "E",
        "query": "diversification and ETFs explained for beginners",
        "fallback": [("felix", "diversification"), ("bagel", "ETF"), ("twocents", "index funds")],
        "body": [
            "Diversification means not putting all your eggs in one basket. If you own 1 stock and it drops 40%, you're down 40%. If you own 20 and one drops 40%, you're down about 2%.",
            "An ETF (exchange-traded fund) is a basket of many stocks you can buy in one trade. SPY holds the 500 biggest US companies; QQQ holds 100 big NASDAQ companies.",
            "Example: $5,000 in SPY spreads your money across 500 companies. Your starter position in Ticker Tycoon is $1,000 of SPY for exactly this reason.",
            "In the Funds tab you can build your own ETF-style basket (for example 40% SPY, 30% AAPL, 30% BTC) and invest in it with one click.",
        ],
        "practice": {"symbol": "S:QQQ", "side": "long", "amount": 1500, "hint": "Buy QQQ: one trade, 100 companies."},
        "quiz": [
            {"q": "Why diversify?", "options": ["To guarantee profits", "To reduce the damage from any single loser", "To pay fewer fees", "To trade faster"], "a": 1, "why": "One bad stock hurts less."},
            {"q": "SPY is...", "options": ["A single company", "An ETF of ~500 large US companies", "A cryptocurrency", "A bond"], "a": 1, "why": "SPY tracks the S&P 500."},
            {"q": "1 of your 20 equal holdings falls 40%. Your portfolio falls about...", "options": ["40%", "20%", "2%", "0%"], "a": 2, "why": "1/20 of 40% = 2%."},
        ],
    },
    {
        "id": "bots", "title": "Trading bots & strategies", "emoji": "B",
        "query": "trading strategies explained DCA moving average RSI grid",
        "fallback": [("rayner", "moving average"), ("investopedia", "dollar cost averaging"), ("wbcrypto", "grid trading")],
        "body": [
            "A trading bot follows fixed rules so you don't have to watch the screen. Ticker Tycoon has five:",
            "DCA (dollar-cost averaging): buys a fixed $ amount on a schedule, e.g. $100 of BTC every hour, no matter the price. MA CROSSOVER: buys when the fast 9-bar average crosses above the slow 21-bar average. RSI DIP BUYER: buys when RSI drops below 30 (oversold) and sells above 70.",
            "MOMENTUM: buys after a strong run up (+1.5%) and exits with a 2% trailing stop. GRID (crypto): places buy levels 1%, 2%, 3%... below the start price and sells each lot 1% higher, profiting from up-and-down chop.",
            "No strategy wins all the time. DCA shines in long uptrends, grids in sideways markets, crossovers in strong trends. Bots keep trading while the game is closed.",
        ],
        "practice": {"tab": "bots", "hint": "Open the Bots tab to see each strategy and its price."},
        "quiz": [
            {"q": "A DCA bot...", "options": ["Buys a fixed amount on a schedule", "Only buys at the bottom", "Uses 10x leverage", "Shorts everything"], "a": 0, "why": "That's dollar-cost averaging."},
            {"q": "RSI below 30 usually means...", "options": ["Overbought", "Oversold", "No volume", "Market closed"], "a": 1, "why": "Below 30 = oversold."},
            {"q": "Grid bots work best when the market is...", "options": ["Trending hard up", "Moving sideways with chop", "Closed", "Crashing nonstop"], "a": 1, "why": "Grids harvest back-and-forth moves."},
        ],
    },
    {
        "id": "mistakes", "title": "Common beginner mistakes", "emoji": "!",
        "query": "common beginner trading mistakes to avoid",
        "fallback": [("humbled", "beginner mistakes"), ("bagel", "investing mistakes"), ("cnbc", "investing mistakes")],
        "body": [
            "1) No stop loss: one bad trade wipes out ten good ones. 2) Too much size or leverage: going all-in on 10x is gambling, not trading.",
            "3) Revenge trading: doubling down right after a loss to 'win it back'. 4) FOMO: buying something AFTER it already jumped 40% because everyone is talking about it.",
            "5) Moving your stop further away when price gets close. 6) Not diversifying. 7) Trading what you don't understand: if you can't explain why you're in, get out.",
            "Example: a player with $10,000 puts $8,000 into a meme coin at 10x. A normal 10% dip liquidates it and $8,000 is gone. The same player risking 1% per trade would have lost $100.",
        ],
        "practice": {"symbol": "C:dogecoin", "side": "long", "amount": 300, "sl_pct": 8, "hint": "Even a meme coin is fine with a small size and a stop loss."},
        "quiz": [
            {"q": "Revenge trading is...", "options": ["A smart strategy", "Trading bigger right after a loss to win it back", "A type of bot", "Shorting a company you dislike"], "a": 1, "why": "It's emotional and usually makes losses bigger."},
            {"q": "Price is close to your stop. Best move?", "options": ["Move the stop further away", "Let the stop do its job", "Add 10x leverage", "Delete the stop"], "a": 1, "why": "Stops exist for exactly this moment."},
            {"q": "FOMO means...", "options": ["Fear of missing out", "Fund of many options", "First order market open", "Fast online money order"], "a": 0, "why": "Chasing a move you're scared to miss."},
        ],
    },
]

HOW_TO_PLAY = [
    ("Live market", "Everything uses real prices in real time: the ticker you're viewing refreshes every few seconds, and your portfolio moves with the actual market. It keeps running while the game is closed (stops, take-profits, dividends and bots are settled when you come back)."),
    ("Search & filters", "Type any ticker or name (AAPL, nvidia, doge, bitcoin). Chips filter Stocks, ETFs, Crypto, Sector, Top movers and your Watchlist. Click the star on a ticker to add it to your watchlist."),
    ("Chart", "Candles from real data. Timeframes 1m / 5m / 1h / 1D / 1W. Mouse wheel zooms, drag scrolls. Volume bars sit at the bottom. Dashed lines show your entry, stop loss (red) and take profit (green); drag them to move."),
    ("Order panel", "Choose $ amount or shares, use 25/50/75/100% of your cash, pick leverage (1x, 2x, 5x or 10x), set optional stop loss and take profit (type a price, a %, or click 'Set on chart' then click the chart). Then press LONG (bet up) or SHORT (bet down)."),
    ("Positions", "Every open trade shows live P&L, value and, for leveraged or short trades, its liquidation price. Close 25/50/100% or edit stops. In the Portfolio tab you see everything, plus queued orders."),
    ("Market hours", "Stocks follow real NYSE hours (9:30-16:00 New York, weekdays, minus holidays). Orders placed while closed are queued and fill at the open. Crypto trades 24/7."),
    ("Margin calls & liquidation", "Leveraged and short positions get an orange warning bar when they've lost 50% of their margin. Add margin or close. At 90% they are liquidated automatically."),
    ("XP & ranks", "Every closed trade earns XP (more for profits, a little for a smart stop-out). Lessons and quizzes give bonus XP. Nothing is locked: XP just raises your level and rank, from Rookie to Ticker Tycoon, and earns achievements."),
    ("Bots", "Buy a bot with in-game cash, give it a budget and up to 5 tickers. It trades automatically using its strategy, even while the game is closed. Selling a bot returns its cash plus half its price."),
    ("Funds", "Build an ETF-style basket with weights, invest any amount in one click and withdraw anytime."),
    ("Dividends", "Hold a real dividend stock through its real ex-dividend date and the cash lands in your account (shorts pay it)."),
    ("While you were away", "When you reopen the game it settles everything that happened: stops and take-profits that hit, dividends, bot trades and queued orders, then shows a summary."),
    ("News & codes", "Press N (or the News button) for real headlines on the ticker you're viewing. Redeem codes there too. Try WELCOME."),
    ("Keyboard", "B = long, S = short, C = close position, E = edit stops, N = news/codes, / = search, ? = help, Esc = back/close."),
]


def _fallback_videos(lesson: dict) -> List[dict]:
    out = []
    for key, topic in lesson["fallback"]:
        name, handle = CHANNELS[key]
        out.append({"id": None, "title": "%s: %s" % (name, topic.title()), "channel": name,
                    "url": "https://www.youtube.com/%s/search?query=%s" % (handle, urllib.parse.quote(topic)),
                    "thumb": None, "fallback": True})
    out.append({"id": None, "title": "Search YouTube: " + lesson["query"], "channel": "YouTube",
                "url": "https://www.youtube.com/results?search_query=" + urllib.parse.quote(lesson["query"]),
                "thumb": None, "fallback": True})
    return out


def lessons_view(owner: str = "live") -> dict:
    prog = {r["id"]: r for r in db.q("SELECT * FROM lessons WHERE owner=?", (owner,))}
    out = []
    for l in LESSONS:
        p = prog.get(l["id"], {})
        out.append({"id": l["id"], "title": l["title"], "emoji": l["emoji"], "body": l["body"],
                    "practice": l["practice"], "completed": bool(p.get("completed")),
                    "quiz_passed": bool(p.get("quiz_passed")),
                    "quiz": [{"q": q["q"], "options": q["options"]} for q in l["quiz"]]})
    done = sum(1 for l in out if l["completed"])
    return {"lessons": out, "done": done, "total": len(out), "how_to_play": HOW_TO_PLAY,
            "youtube_key": bool(fetchers.config.YOUTUBE_API_KEY)}


def _lesson(lid: str) -> dict:
    for l in LESSONS:
        if l["id"] == lid:
            return l
    raise engine.GameError("unknown lesson")


def videos(lid: str) -> dict:
    l = _lesson(lid)
    vids = fetchers.youtube_search(l["query"], 5)
    if vids is None:
        return {"videos": _fallback_videos(l), "source": "fallback",
                "note": "Add YOUTUBE_API_KEY to .env to find and embed the best videos automatically. Showing hand-picked channels meanwhile."}
    if not vids:
        return {"videos": _fallback_videos(l), "source": "fallback",
                "note": "YouTube search failed (quota or network). Showing hand-picked channels."}
    return {"videos": vids, "source": "youtube", "note": None}


def search_videos(q: str) -> dict:
    q = (q or "").strip()[:120]
    if not q:
        return {"videos": [], "source": None}
    vids = fetchers.youtube_search(q + " explained", 6)
    if vids is None:
        return {"videos": [{"id": None, "title": "Search YouTube for “%s”" % q, "channel": "YouTube",
                            "url": "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q), "fallback": True}],
                "source": "fallback", "note": "Add YOUTUBE_API_KEY to .env to search and play videos right here."}
    return {"videos": vids, "source": "youtube", "note": None if vids else "No videos found."}


def complete(lid: str, owner: str = "live") -> dict:
    _lesson(lid)
    r = db.q1("SELECT * FROM lessons WHERE owner=? AND id=?", (owner, lid))
    if r and r["completed"]:
        return {"ok": True, "xp": 0}
    db.x("INSERT INTO lessons(owner,id,completed,quiz_passed,ts) VALUES(?,?,1,0,?) "
         "ON CONFLICT(owner,id) DO UPDATE SET completed=1, ts=excluded.ts", (owner, lid, time.time()))
    res = engine.add_xp(LESSON_XP, "lesson", owner)
    _check(owner)
    return {"ok": True, "xp": LESSON_XP, "level_up": res.get("level_up"), "unlocked": res.get("unlocked")}


def quiz(lid: str, answers: List[int], owner: str = "live") -> dict:
    l = _lesson(lid)
    results = []
    for i, q in enumerate(l["quiz"]):
        a = answers[i] if i < len(answers) else None
        results.append({"correct": a == q["a"], "answer": q["a"], "why": q["why"]})
    score = sum(1 for r in results if r["correct"])
    passed = score == len(l["quiz"])
    xp = 0
    res = {}
    if passed:
        r = db.q1("SELECT * FROM lessons WHERE owner=? AND id=?", (owner, lid))
        if not (r and r["quiz_passed"]):
            db.x("INSERT INTO lessons(owner,id,completed,quiz_passed,ts) VALUES(?,?,1,1,?) "
                 "ON CONFLICT(owner,id) DO UPDATE SET quiz_passed=1, completed=1", (owner, lid, time.time()))
            xp = QUIZ_XP + (0 if (r and r["completed"]) else LESSON_XP)
            res = engine.add_xp(xp, "quiz", owner)
    _check(owner)
    return {"score": score, "total": len(l["quiz"]), "passed": passed, "results": results, "xp": xp,
            "level_up": res.get("level_up"), "unlocked": res.get("unlocked")}


def _check(owner: str = "live") -> None:
    rows = db.q("SELECT * FROM lessons WHERE owner=?", (owner,))
    if sum(1 for r in rows if r["completed"]) >= len(LESSONS):
        engine._grant("scholar", owner)
    if sum(1 for r in rows if r["quiz_passed"]) >= len(LESSONS):
        engine._grant("quiz_whiz", owner)
