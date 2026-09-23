import os
import json
import hashlib
import html
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
import feedparser
from google import genai

# ============================================================
# Gold Update & News 24/7
# Reliable market-data version
#
# Required GitHub Secrets:
#   GEMINI_API_KEY
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHANNEL_USERNAME
#
# Optional:
#   POST_MODE = test | market | news | breaking
# ============================================================

GEMINI_MODEL = "gemini-3.6-flash"
STATE_FILE = "state.json"

GOLD_SPOT_URL = "https://xaus.com/api/v1/spot?compact=1"
GOLD_CHART_URL = "https://xaus.com/api/v1/chart"
BTC_KLINES_URL = "https://api.binance.com/api/v3/klines"
BTC_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"

NEWS_QUERIES = [
    "gold XAUUSD",
    "Federal Reserve interest rates",
    "US economy inflation jobs",
    "Bitcoin cryptocurrency",
    "geopolitics Middle East",
]

TRUSTED_DOMAINS = {
    "reuters.com", "apnews.com", "bloomberg.com", "bbc.com",
    "cnbc.com", "ft.com", "wsj.com", "marketwatch.com",
    "federalreserve.gov", "treasury.gov", "sec.gov", "whitehouse.gov",
    "ecb.europa.eu", "imf.org", "worldbank.org", "gov.uk",
    "europa.eu", "un.org", "opec.org", "gold.org"
}

session = requests.Session()
session.headers.update({
    "User-Agent": "GoldUpdateNews24-7/1.0 (+https://github.com/)"
})


# ----------------------------
# Basic helpers
# ----------------------------

def utc_now():
    return datetime.now(timezone.utc)


def utc_text(dt=None):
    dt = dt or utc_now()
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def get_env(name, required=True):
    value = os.getenv(name, "").strip()
    if required and not value:
        raise RuntimeError(f"Missing GitHub Secret/environment variable: {name}")
    return value


def http_json(url, params=None, timeout=20):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ----------------------------
# Persistent state
# ----------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "posted_news": [],
            "breaking_today": 0,
            "breaking_date": "",
            "last_test": ""
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}

    state.setdefault("posted_news", [])
    state.setdefault("breaking_today", 0)
    state.setdefault("breaking_date", "")
    state.setdefault("last_test", "")
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def reset_daily_counter(state):
    today = utc_now().strftime("%Y-%m-%d")
    if state.get("breaking_date") != today:
        state["breaking_date"] = today
        state["breaking_today"] = 0


# ----------------------------
# Telegram
# ----------------------------

def telegram_send(text):
    token = get_env("TELEGRAM_BOT_TOKEN")
    chat = get_env("TELEGRAM_CHANNEL_USERNAME")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram text limit is 4096 characters.
    if len(text) > 4000:
        text = text[:3990].rstrip() + "\n…"

    r = session.post(
        url,
        json={
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": False
        },
        timeout=30
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


# ----------------------------
# Market data
# ----------------------------

def get_gold_spot():
    data = http_json(GOLD_SPOT_URL)
    price = data.get("spot_usd_oz")
    state = data.get("data_state", {})
    updated = data.get("updated_at") or state.get("as_of")

    if not isinstance(price, (int, float)) or price <= 0:
        raise RuntimeError("Gold spot API returned no valid XAU/USD price.")

    return {
        "price": float(price),
        "updated_at": updated,
        "data_state": state.get("status", "unknown"),
        "source": "XAUS XAU/USD spot"
    }


def get_gold_candles(interval="15m", range_="5d"):
    data = http_json(
        GOLD_CHART_URL,
        params={"symbol": "xau", "range": range_, "interval": interval}
    )

    # XAUS chart response is a list of OHLCV points: t,o,h,l,c,v
    points = data.get("points") or data.get("bars") or []

    candles = []
    for p in points:
        try:
            close = float(p["c"])
            high = float(p["h"])
            low = float(p["l"])
            op = float(p["o"])
            volume = float(p.get("v") or 0)
            candles.append({
                "t": int(p["t"]),
                "open": op,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume
            })
        except (KeyError, TypeError, ValueError):
            continue

    if len(candles) < 30:
        raise RuntimeError("Not enough real Gold OHLC candles returned.")

    return candles


def get_btc_data(interval="15m", limit=300):
    # Binance public market-data endpoint; no API key required.
    raw = http_json(
        BTC_KLINES_URL,
        params={"symbol": "BTCUSDT", "interval": interval, "limit": limit}
    )
    ticker = http_json(
        BTC_TICKER_URL,
        params={"symbol": "BTCUSDT"}
    )

    candles = []
    for p in raw:
        candles.append({
            "t": int(p[0] / 1000),
            "open": float(p[1]),
            "high": float(p[2]),
            "low": float(p[3]),
            "close": float(p[4]),
            "volume": float(p[5])
        })

    price = float(ticker["lastPrice"])
    change_pct = float(ticker["priceChangePercent"])

    if len(candles) < 30 or price <= 0:
        raise RuntimeError("Not enough real BTC market data returned.")

    return {
        "price": price,
        "change_pct": change_pct,
        "candles": candles,
        "source": "Binance BTC/USDT market data"
    }


# ----------------------------
# Technical calculations
# ----------------------------

def pivot_levels(candles, lookback=120):
    c = candles[-lookback:]

    highs = []
    lows = []

    # A level is considered only when it is an actual local pivot.
    for i in range(2, len(c) - 2):
        h = c[i]["high"]
        l = c[i]["low"]

        if h >= c[i-1]["high"] and h >= c[i-2]["high"] and \
           h >= c[i+1]["high"] and h >= c[i+2]["high"]:
            highs.append(h)

        if l <= c[i-1]["low"] and l <= c[i-2]["low"] and \
           l <= c[i+1]["low"] and l <= c[i+2]["low"]:
            lows.append(l)

    return highs, lows


def cluster_levels(levels, tolerance):
    levels = sorted(levels)
    clusters = []

    for level in levels:
        if not clusters:
            clusters.append([level])
            continue

        center = sum(clusters[-1]) / len(clusters[-1])
        if abs(level - center) <= tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])

    return [sum(x) / len(x) for x in clusters if x]


def nearest_levels(candles, price, tolerance, count=2):
    highs, lows = pivot_levels(candles)

    resistance_candidates = [
        x for x in cluster_levels(highs, tolerance) if x > price
    ]
    support_candidates = [
        x for x in cluster_levels(lows, tolerance) if x < price
    ]

    resistance = sorted(resistance_candidates)[:count]
    support = sorted(support_candidates, reverse=True)[:count]

    return support, resistance


def structure(candles):
    c = candles[-80:]
    highs = [x["high"] for x in c]
    lows = [x["low"] for x in c]

    first_half_high = max(highs[:40])
    second_half_high = max(highs[40:])
    first_half_low = min(lows[:40])
    second_half_low = min(lows[40:])

    if second_half_high > first_half_high and second_half_low > first_half_low:
        return "Bullish"
    if second_half_high < first_half_high and second_half_low < first_half_low:
        return "Bearish"
    return "Mixed"


def pct_change(candles, bars=16):
    if len(candles) <= bars:
        return 0.0
    old = candles[-bars-1]["close"]
    new = candles[-1]["close"]
    return ((new - old) / old) * 100


def format_price(symbol, value):
    if symbol == "BTC":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def format_zone(symbol, value, width):
    if symbol == "BTC":
        a = round(value - width)
        b = round(value + width)
        return f"{a:,}–{b:,}"
    a = round(value - width, 2)
    b = round(value + width, 2)
    return f"{a:,.2f}–{b:,.2f}"


def market_snapshot():
    gold = get_gold_spot()
    gold_c = get_gold_candles("15m", "5d")
    btc = get_btc_data("15m", 300)

    gold_support, gold_resistance = nearest_levels(
        gold_c, gold["price"], tolerance=5.0, count=2
    )
    btc_support, btc_resistance = nearest_levels(
        btc["candles"], btc["price"], tolerance=100.0, count=2
    )

    return {
        "time_utc": utc_text(),
        "gold": {
            "price": gold["price"],
            "change_pct": pct_change(gold_c, 16),
            "structure": structure(gold_c),
            "support": gold_support,
            "resistance": gold_resistance,
            "data_updated_at": gold["updated_at"],
            "data_state": gold["data_state"],
            "source": gold["source"]
        },
        "btc": {
            "price": btc["price"],
            "change_pct_24h": btc["change_pct"],
            "structure": structure(btc["candles"]),
            "support": btc_support,
            "resistance": btc_resistance,
            "source": btc["source"]
        }
    }


# ----------------------------
# Gemini writing
# ----------------------------

def gemini_client():
    return genai.Client(api_key=get_env("GEMINI_API_KEY"))


def ai_text(prompt):
    client = gemini_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned empty text.")
    return text.strip()


def clean_ai_text(text):
    # Keep Telegram output plain and compact.
    text = text.replace("**", "").replace("__", "")
    text = text.replace("```", "")
    return text.strip()


def market_post(snapshot):
    g = snapshot["gold"]
    b = snapshot["btc"]

    prompt = f"""
You are the editor of a professional financial Telegram channel.

Write ONE short market update from ONLY the verified data below.
Do not invent numbers, news, levels, reasons, or forecasts.
Do not give entry, TP, SL, or trade signals.
Support/resistance is only for audience reference.

Use exactly this information:
UTC time: {snapshot["time_utc"]}

GOLD XAU/USD:
Price: {g["price"]:.2f}
Recent 15m change: {g["change_pct"]:.2f}%
Structure: {g["structure"]}
Support: {g["support"]}
Resistance: {g["resistance"]}
Data state: {g["data_state"]}

BTC/USDT:
Price: {b["price"]:.0f}
24h change: {b["change_pct_24h"]:.2f}%
Structure: {b["structure"]}
Support: {b["support"]}
Resistance: {b["resistance"]}

Rules:
- Preserve every supplied number exactly.
- Simple English.
- Maximum about 900 characters.
- No prediction.
- No trading advice.
- No fake "institutional" claims.
- No unnecessary paragraphs.
- Use this structure:

Gold Update
Price:
Structure:
Support:
Resistance:

Bitcoin Update
Price:
24h:
Structure:
Support:
Resistance:

Time: ...
"""

    return clean_ai_text(ai_text(prompt))


# ----------------------------
# News collection
# ----------------------------

def domain_of(url):
    try:
        host = url.split("://", 1)[1].split("/", 1)[0].lower()
        host = host.removeprefix("www.")
        return host
    except Exception:
        return ""


def trusted_source(url, source_title=""):
    d = domain_of(url)
    if any(d == x or d.endswith("." + x) for x in TRUSTED_DOMAINS):
        return True
    return False


def news_feed(query):
    url = (
        "https://news.google.com/rss/search?"
        + "q=" + quote(query + " when:1d")
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    return feedparser.parse(url)


def collect_news(max_items=20):
    items = []
    seen = set()

    for query in NEWS_QUERIES:
        feed = news_feed(query)

        for entry in feed.entries[:12]:
            title = html.unescape(entry.get("title", "")).strip()
            link = entry.get("link", "").strip()

            source = ""
            if hasattr(entry, "source"):
                source = getattr(entry.source, "title", "") or ""

            if not title or not link:
                continue

            # Google News normally exposes the original publisher
            # in source.title; reject items where we cannot verify source.
            if not source:
                continue

            key = hashlib.sha256(
                (title + "|" + source).encode("utf-8")
            ).hexdigest()

            if key in seen:
                continue

            seen.add(key)

            items.append({
                "id": key,
                "title": title,
                "google_link": link,
                "source": source
            })

            if len(items) >= max_items:
                return items

    return items


def select_news(items, breaking=False):
    if not items:
        return None

    compact = "\n".join(
        f'{i+1}. {x["title"]} | Source: {x["source"]}'
        for i, x in enumerate(items)
    )

    prompt = f"""
You are a strict financial news editor.

Select ONE genuinely important current story from the list below.
Do not invent or combine stories.
Prefer stories directly relevant to Gold, Bitcoin, rates, inflation,
major economic data, central banks, or significant geopolitics.

Breaking mode: {breaking}

Return ONLY the number of the selected item.
If none is genuinely important, return 0.

NEWS:
{compact}
"""

    answer = ai_text(prompt)
    m = re.search(r"\b(\d+)\b", answer)
    if not m:
        return None

    n = int(m.group(1))
    if n < 1 or n > len(items):
        return None

    return items[n - 1]


def news_post(item, breaking=False):
    label = "BREAKING NEWS" if breaking else "MARKET NEWS"

    prompt = f"""
Write a short Telegram financial news post.

Source title: {item["source"]}
Headline: {item["title"]}

Rules:
- Do not add facts not contained in the headline.
- Do not change reported/said/expected into confirmed facts.
- Preserve names, numbers, countries, dates and quantities exactly if present.
- Simple English.
- Maximum 650 characters.
- No opinion.
- No prediction.
- End with the source name.
- Use this format:

{label}

[2-4 short sentences explaining only the supplied headline.]

Source: {item["source"]}
"""

    body = clean_ai_text(ai_text(prompt))

    # Source link must be visible under every news post.
    return body + f'\n\nSource: {item["source"]}\n{item["google_link"]}'


# ----------------------------
# Modes
# ----------------------------

def run_test():
    print("====================================")
    print("Gold Update & News 24/7 Bot")
    print("Mode: test")
    print(f"UTC: {utc_text()}")
    print("====================================")

    snapshot = market_snapshot()
    post = market_post(snapshot)

    print(post)
    print("\nTEST PASSED: market data and Gemini response are working.")


def run_market():
    snapshot = market_snapshot()
    post = market_post(snapshot)
    telegram_send(post)

    state = load_state()
    state["last_market"] = utc_now().isoformat()
    save_state(state)


def run_news():
    state = load_state()
    items = collect_news()
    selected = select_news(items, breaking=False)

    if not selected:
        print("No suitable news selected. No post sent.")
        return

    if selected["id"] in state["posted_news"]:
        print("Selected news already posted. No duplicate sent.")
        return

    post = news_post(selected, breaking=False)
    telegram_send(post)

    state["posted_news"] = (
        state["posted_news"] + [selected["id"]]
    )[-100:]
    save_state(state)


def run_breaking():
    state = load_state()
    reset_daily_counter(state)

    if state["breaking_today"] >= 5:
        print("Daily breaking-news limit reached. No post.")
        save_state(state)
        return

    items = collect_news()
    selected = select_news(items, breaking=True)

    if not selected:
        print("No genuinely important breaking story. No post.")
        save_state(state)
        return

    if selected["id"] in state["posted_news"]:
        print("Breaking story already posted. No duplicate.")
        save_state(state)
        return

    post = news_post(selected, breaking=True)
    telegram_send(post)

    state["posted_news"] = (
        state["posted_news"] + [selected["id"]]
    )[-100:]
    state["breaking_today"] += 1
    save_state(state)


def main():
    mode = os.getenv("POST_MODE", "test").strip().lower()

    if mode == "test":
        run_test()
    elif mode == "market":
        run_market()
    elif mode == "news":
        run_news()
    elif mode == "breaking":
        run_breaking()
    else:
        raise RuntimeError(
            f"Unknown POST_MODE={mode}. Use test, market, news, or breaking."
        )


if __name__ == "__main__":
    main()
