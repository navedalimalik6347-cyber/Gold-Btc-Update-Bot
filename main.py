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

GOLD_PRICE_URL = "https://api.goldprice.dev/v1/prices"
GOLD_BARS_URL = "https://api.goldprice.dev/v1/bars"
XAUS_SPOT_URL = "https://xaus.com/api/v1/spot?compact=1"
XAUS_CHART_URL = "https://xaus.com/api/v1/chart"
BTC_COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
BTC_COINBASE_TICKER_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
BTC_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
BTC_KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"

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
    # Primary: goldprice.dev anonymous XAU/USD spot endpoint.
    # Fallback: XAUS keyless XAU/USD spot endpoint.
    errors = []

    for url, source_name in [
        (GOLD_PRICE_URL, "GoldPrice.dev XAU/USD spot"),
        (XAUS_SPOT_URL, "XAUS XAU/USD spot"),
    ]:
        try:
            if "goldprice.dev" in url:
                data = http_json(
                    url,
                    params={"symbol": "XAU-USD-SPOT"},
                    timeout=15,
                )
                row = (data.get("symbols") or [{}])[0]
                price = float(row.get("price"))
                updated = row.get("computed_at")
                stale = bool(row.get("is_stale", False))

                if price <= 0 or stale:
                    raise RuntimeError(
                        f"GoldPrice.dev returned invalid/stale data: {row}"
                    )

                return {
                    "price": price,
                    "updated_at": updated,
                    "data_state": "stale" if stale else "fresh",
                    "source": source_name,
                }

            data = http_json(url, timeout=15)
            price = data.get("spot_usd_oz")
            state = data.get("data_state", {})
            updated = data.get("updated_at") or state.get("as_of")

            if not isinstance(price, (int, float)) or price <= 0:
                raise RuntimeError("XAUS returned no valid XAU/USD price.")

            return {
                "price": float(price),
                "updated_at": updated,
                "data_state": state.get("status", "unknown"),
                "source": source_name,
            }

        except Exception as exc:
            errors.append(f"{source_name}: {exc}")

    raise RuntimeError(
        "Gold price data unavailable from all configured sources.\n"
        + "\n".join(errors)
    )


def _parse_goldprice_bars(data):
    bars = data.get("bars") or []
    candles = []

    for p in bars:
        try:
            candles.append({
                "t": int(datetime.fromisoformat(
                    p["bar_start"].replace("Z", "+00:00")
                ).timestamp()),
                "open": float(p["open"]),
                "high": float(p["high"]),
                "low": float(p["low"]),
                "close": float(p["close"]),
                "volume": 0.0,
            })
        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["t"])
    return candles


def get_gold_candles(interval="15m", range_="5d"):
    # Try XAUS intraday first. If it is unavailable, use the free
    # GoldPrice.dev daily XAU/USD bars as a real-data fallback.
    try:
        data = http_json(
            XAUS_CHART_URL,
            params={"symbol": "xau", "range": range_, "interval": interval},
            timeout=15,
        )

        points = data.get("points") or data.get("bars") or []
        candles = []

        for p in points:
            try:
                candles.append({
                    "t": int(p["t"]),
                    "open": float(p["o"]),
                    "high": float(p["h"]),
                    "low": float(p["l"]),
                    "close": float(p["c"]),
                    "volume": float(p.get("v") or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue

        if len(candles) >= 30:
            return candles
    except Exception:
        pass

    # GoldPrice.dev free tier provides 30 days of daily XAU/USD OHLC.
    today = utc_now().date()
    start = today - timedelta(days=29)

    data = http_json(
        GOLD_BARS_URL,
        params={
            "symbol": "XAU-USD-SPOT",
            "interval": "1d",
            "from": start.isoformat() + "T00:00:00Z",
            "to": today.isoformat() + "T23:59:59Z",
            "limit": 100,
        },
        timeout=15,
    )

    candles = _parse_goldprice_bars(data)

    if len(candles) < 10:
        raise RuntimeError("Not enough real Gold OHLC data returned.")

    return candles

def get_btc_data(interval="15m", limit=300):
    """Get real BTC/USD market data without Binance.

    GitHub Actions is currently receiving HTTP 451 from Binance's public API.
    We therefore use Coinbase public market data first, with Kraken as a
    second public-data fallback. No API key is required for either path.
    """
    granularity_map = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "6h": 21600,
        "1d": 86400,
    }
    granularity = granularity_map.get(interval, 900)

    # ----------------------------
    # Primary: Coinbase Exchange
    # ----------------------------
    try:
        raw = http_json(
            BTC_COINBASE_CANDLES_URL,
            params={"granularity": granularity},
            timeout=15,
        )

        candles = []
        for p in raw:
            if not isinstance(p, list) or len(p) < 6:
                continue
            # Coinbase candle format: [time, low, high, open, close, volume]
            candles.append({
                "t": int(p[0]),
                "open": float(p[3]),
                "high": float(p[2]),
                "low": float(p[1]),
                "close": float(p[4]),
                "volume": float(p[5]),
            })

        candles.sort(key=lambda x: x["t"])

        ticker = http_json(BTC_COINBASE_TICKER_URL, timeout=15)
        price = float(ticker.get("price", 0))

        if price <= 0 and candles:
            price = candles[-1]["close"]

        # 24h change from 15m candles: 96 bars = 24 hours.
        if len(candles) >= 97:
            base = candles[-97]["close"]
            change_pct = ((price - base) / base) * 100 if base else 0.0
        else:
            change_pct = 0.0

        if len(candles) >= 30 and price > 0:
            return {
                "price": price,
                "change_pct": change_pct,
                "candles": candles[-limit:],
                "source": "Coinbase BTC/USD public market data",
            }
    except Exception as coinbase_error:
        coinbase_message = str(coinbase_error)
    else:
        coinbase_message = "Coinbase returned insufficient BTC data."

    # ----------------------------
    # Fallback: Kraken public API
    # ----------------------------
    try:
        raw = http_json(
            BTC_KRAKEN_OHLC_URL,
            params={"pair": "XBTUSD", "interval": 15},
            timeout=15,
        )

        if raw.get("error"):
            raise RuntimeError("Kraken OHLC error: " + ", ".join(raw["error"]))

        result = raw.get("result", {})
        rows = result.get("XXBTZUSD") or result.get("XBTUSD")
        if not rows:
            # Kraken may return the actual pair key under a different name.
            rows = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), None)

        candles = []
        for p in rows or []:
            if len(p) < 7:
                continue
            candles.append({
                "t": int(float(p[0])),
                "open": float(p[1]),
                "high": float(p[2]),
                "low": float(p[3]),
                "close": float(p[4]),
                "volume": float(p[6]),
            })

        candles.sort(key=lambda x: x["t"])

        ticker_raw = http_json(
            BTC_KRAKEN_TICKER_URL,
            params={"pair": "XBTUSD"},
            timeout=15,
        )
        ticker_result = ticker_raw.get("result", {})
        ticker_data = next(iter(ticker_result.values()), {}) if ticker_result else {}
        price = float((ticker_data.get("c") or [0])[0])

        if price <= 0 and candles:
            price = candles[-1]["close"]

        if len(candles) >= 97:
            base = candles[-97]["close"]
            change_pct = ((price - base) / base) * 100 if base else 0.0
        else:
            change_pct = 0.0

        if len(candles) >= 30 and price > 0:
            return {
                "price": price,
                "change_pct": change_pct,
                "candles": candles[-limit:],
                "source": "Kraken BTC/USD public market data",
            }
    except Exception as kraken_error:
        raise RuntimeError(
            "BTC market data unavailable. "
            f"Coinbase error: {coinbase_message}; "
            f"Kraken error: {kraken_error}"
        )

    raise RuntimeError(
        "BTC market data unavailable. "
        f"Coinbase error: {coinbase_message}; "
        "Kraken returned insufficient data."
    )


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
Recent change: {g["change_pct"]:.2f}%
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
