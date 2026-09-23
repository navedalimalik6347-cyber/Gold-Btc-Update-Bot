import os
import re
import json
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests
import yfinance as yf
from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_USERNAME = os.environ.get("TELEGRAM_CHANNEL_USERNAME")

POST_MODE = os.environ.get("POST_MODE", "market").lower()

STATE_FILE = "state.json"

# Maximum breaking-news posts in one UTC day
MAX_BREAKING_PER_DAY = 5

# Keep recent news IDs for duplicate protection
MAX_STORED_NEWS_IDS = 100


if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing.")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing.")

if not TELEGRAM_CHANNEL_USERNAME:
    raise ValueError("TELEGRAM_CHANNEL_USERNAME is missing.")


client_ai = genai.Client(api_key=GEMINI_API_KEY)


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_string():
    return utc_now().strftime("%Y-%m-%d %H:%M UTC")


# ============================================================
# STATE / DUPLICATE PROTECTION
# ============================================================

def load_state():
    default_state = {
        "breaking_date": "",
        "breaking_count": 0,
        "news_ids": []
    }

    if not os.path.exists(STATE_FILE):
        return default_state

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default_state

        state.setdefault("breaking_date", "")
        state.setdefault("breaking_count", 0)
        state.setdefault("news_ids", [])

        return state

    except Exception:
        return default_state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def reset_breaking_counter_if_new_day(state):
    today = utc_now().strftime("%Y-%m-%d")

    if state.get("breaking_date") != today:
        state["breaking_date"] = today
        state["breaking_count"] = 0

    return state


def make_news_id(title, link):
    raw = f"{title.strip().lower()}|{link.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ============================================================
# TELEGRAM
# ============================================================

def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHANNEL_USERNAME,
        "text": message,
        "disable_web_page_preview": False
    }

    response = requests.post(
        url,
        json=payload,
        timeout=30
    )

    if response.status_code != 200:
        raise Exception(
            f"Telegram error: {response.status_code} - {response.text}"
        )

    print("Telegram post sent successfully.")


# ============================================================
# MARKET DATA
# ============================================================

def get_symbol_data(symbol, period="5d", interval="1h"):
    try:
        ticker = yf.Ticker(symbol)

        data = ticker.history(
            period=period,
            interval=interval,
            auto_adjust=False,
            prepost=True
        )

        if data is None or data.empty:
            return None

        data = data.dropna(subset=["Close"])

        return data

    except Exception as e:
        print(f"Market data error for {symbol}: {e}")
        return None


def get_current_price(symbol):
    """
    Gets the latest available intraday price from Yahoo Finance.
    This is NOT guaranteed to match TradingView tick-for-tick.
    """

    data = get_symbol_data(
        symbol,
        period="2d",
        interval="5m"
    )

    if data is None or data.empty:
        return None

    latest = data.iloc[-1]

    price = float(latest["Close"])

    timestamp = data.index[-1]

    return {
        "price": price,
        "timestamp": timestamp
    }


# ============================================================
# PRICE / ZONE FORMATTING
# ============================================================

def gold_decimals(value):
    return f"{value:.2f}"


def btc_decimals(value):
    return f"{value:,.0f}"


def calculate_atr(data, period=14):
    if data is None or len(data) < period + 2:
        return None

    high = data["High"]
    low = data["Low"]
    close = data["Close"]

    previous_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - previous_close).abs()
    tr3 = (low - previous_close).abs()

    true_range = tr1.combine(tr2, max).combine(tr3, max)

    atr = true_range.rolling(period).mean().iloc[-1]

    if atr != atr:
        return None

    return float(atr)


def find_support_resistance(data, price, asset):
    """
    Creates small informational reference zones.

    IMPORTANT:
    These are NOT entry zones.
    They are only nearby technical reference areas.

    Gold:
        zone width approximately 5 dollars

    BTC:
        zone width approximately 100 dollars
    """

    if data is None or len(data) < 30:
        return None

    # Use recent candles
    recent = data.tail(80)

    # Recent swing highs/lows
    swing_highs = []
    swing_lows = []

    highs = recent["High"].tolist()
    lows = recent["Low"].tolist()

    for i in range(2, len(recent) - 2):

        if (
            highs[i] > highs[i - 1]
            and highs[i] > highs[i - 2]
            and highs[i] > highs[i + 1]
            and highs[i] > highs[i + 2]
        ):
            swing_highs.append(highs[i])

        if (
            lows[i] < lows[i - 1]
            and lows[i] < lows[i - 2]
            and lows[i] < lows[i + 1]
            and lows[i] < lows[i + 2]
        ):
            swing_lows.append(lows[i])

    if asset == "gold":
        zone_width = 5.0
    else:
        zone_width = 100.0

    supports = [
        x for x in swing_lows
        if x < price
    ]

    resistances = [
        x for x in swing_highs
        if x > price
    ]

    supports = sorted(
        supports,
        key=lambda x: abs(price - x)
    )

    resistances = sorted(
        resistances,
        key=lambda x: abs(price - x)
    )

    support = supports[0] if supports else price - zone_width
    resistance = resistances[0] if resistances else price + zone_width

    return {
        "support": float(support),
        "resistance": float(resistance),
        "zone_width": zone_width
    }


def format_zone(level, width, asset):
    if asset == "gold":
        low = round(level, 2)
        high = round(level + width, 2)

        return f"${low:.2f}–${high:.2f}"

    low = round(level / 100) * 100
    high = low + int(width)

    return f"${low:,.0f}–${high:,.0f}"


def calculate_structure(data):
    """
    Simple objective structure calculation.

    Uses recent closes:
      Higher highs + higher lows -> Bullish
      Lower highs + lower lows -> Bearish
      Otherwise -> Range / Mixed
    """

    if data is None or len(data) < 30:
        return "Mixed / Range"

    recent = data.tail(30)

    first_close = float(recent["Close"].iloc[0])
    last_close = float(recent["Close"].iloc[-1])

    first_high = float(recent["High"].iloc[:10].max())
    last_high = float(recent["High"].iloc[-10:].max())

    first_low = float(recent["Low"].iloc[:10].min())
    last_low = float(recent["Low"].iloc[-10:].min())

    higher_high = last_high > first_high
    higher_low = last_low > first_low

    lower_high = last_high < first_high
    lower_low = last_low < first_low

    if higher_high and higher_low and last_close > first_close:
        return "Bullish"

    if lower_high and lower_low and last_close < first_close:
        return "Bearish"

    return "Mixed / Range"


def get_market_snapshot():
    # XAUUSD=X is used as the Yahoo Finance XAU/USD reference.
    gold_5m = get_symbol_data(
        "XAUUSD=X",
        period="2d",
        interval="5m"
    )

    btc_5m = get_symbol_data(
        "BTC-USD",
        period="2d",
        interval="5m"
    )

    gold_1h = get_symbol_data(
        "XAUUSD=X",
        period="10d",
        interval="1h"
    )

    btc_1h = get_symbol_data(
        "BTC-USD",
        period="10d",
        interval="1h"
    )

    if gold_5m is None or gold_5m.empty:
        raise Exception("Gold price data unavailable.")

    if btc_5m is None or btc_5m.empty:
        raise Exception("BTC price data unavailable.")

    gold_price = float(gold_5m["Close"].iloc[-1])
    btc_price = float(btc_5m["Close"].iloc[-1])

    gold_sr = find_support_resistance(
        gold_1h,
        gold_price,
        "gold"
    )

    btc_sr = find_support_resistance(
        btc_1h,
        btc_price,
        "btc"
    )

    gold_structure = calculate_structure(gold_1h)
    btc_structure = calculate_structure(btc_1h)

    if gold_sr is None:
        gold_support = "N/A"
        gold_resistance = "N/A"
    else:
        gold_support = format_zone(
            gold_sr["support"],
            5.0,
            "gold"
        )

        gold_resistance = format_zone(
            gold_sr["resistance"],
            5.0,
            "gold"
        )

    if btc_sr is None:
        btc_support = "N/A"
        btc_resistance = "N/A"
    else:
        btc_support = format_zone(
            btc_sr["support"],
            100.0,
            "btc"
        )

        btc_resistance = format_zone(
            btc_sr["resistance"],
            100.0,
            "btc"
        )

    return {
        "gold_price": gold_price,
        "btc_price": btc_price,

        "gold_support": gold_support,
        "gold_resistance": gold_resistance,
        "gold_structure": gold_structure,

        "btc_support": btc_support,
        "btc_resistance": btc_resistance,
        "btc_structure": btc_structure,

        "time": utc_string()
    }


# ============================================================
# GEMINI MARKET POST
# ============================================================

def generate_market_post(snapshot):

    prompt = f"""
You are writing a professional Telegram market update.

Use ONLY the verified data supplied below.

VERIFIED MARKET DATA:

Gold (XAUUSD):
Price: ${snapshot["gold_price"]:.2f}
Structure: {snapshot["gold_structure"]}
Support reference: {snapshot["gold_support"]}
Resistance reference: {snapshot["gold_resistance"]}

Bitcoin (BTC/USD):
Price: ${snapshot["btc_price"]:,.0f}
Structure: {snapshot["btc_structure"]}
Support reference: {snapshot["btc_support"]}
Resistance reference: {snapshot["btc_resistance"]}

Time: {snapshot["time"]}

STRICT RULES:

1. Never change any price.
2. Never invent another price.
3. Never invent support or resistance.
4. Never create an entry, TP or SL.
5. Support and resistance are INFORMATIONAL REFERENCE AREAS ONLY.
6. Do not call them trade zones.
7. Do not say buy or sell.
8. Do not predict the next move.
9. Keep the post short.
10. Do not use Markdown bold.
11. Do not use hashtags.
12. Do not add unnecessary explanations.
13. Use exactly the supplied support/resistance areas.
14. Keep Gold and BTC separate.
15. Use simple professional English.

Return ONLY the Telegram post.

Use this structure:

🟡 GOLD | XAUUSD

Price: ...
Structure: ...
Support: ...
Resistance: ...

₿ BITCOIN | BTC/USD

Price: ...
Structure: ...
Support: ...
Resistance: ...

Time: ...
Source: Market Data

Add one short line:
"Reference levels are for market context only, not trade entries."
"""

    response = client_ai.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=500
        )
    )

    return response.text.strip()


# ============================================================
# NEWS SOURCES
# ============================================================

TRUSTED_DOMAINS = {
    "reuters.com": "Reuters",
    "apnews.com": "AP News",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
    "cnbc.com": "CNBC",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "federalreserve.gov": "Federal Reserve",
    "treasury.gov": "U.S. Treasury",
    "bls.gov": "U.S. Bureau of Labor Statistics",
    "whitehouse.gov": "White House",
    "sec.gov": "SEC",
    "ecb.europa.eu": "European Central Bank",
    "imf.org": "IMF",
    "worldbank.org": "World Bank"
}


def get_source_name(link):
    link_lower = link.lower()

    for domain, name in TRUSTED_DOMAINS.items():
        if domain in link_lower:
            return name

    return None


def google_news_rss(query):
    encoded = quote_plus(query)

    url = (
        "https://news.google.com/rss/search?"
        f"q={encoded}&hl=en-US&gl=US&ceid=US:en"
    )

    try:
        response = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        response.raise_for_status()

        root = ET.fromstring(response.content)

        results = []

        for item in root.findall(".//item"):

            title = item.findtext("title", default="").strip()
            link = item.findtext("link", default="").strip()
            pub_date = item.findtext("pubDate", default="").strip()
            description = item.findtext(
                "description",
                default=""
            ).strip()

            source_element = item.find("source")

            source_name = ""

            if source_element is not None:
                source_name = (
                    source_element.text or ""
                ).strip()

            source_from_link = get_source_name(link)

            if not source_from_link:
                continue

            results.append({
                "title": title,
                "link": link,
                "date": pub_date,
                "description": description,
                "source": source_from_link
            })

        return results

    except Exception as e:
        print(f"RSS error: {e}")
        return []


# ============================================================
# COLLECT NEWS
# ============================================================

def collect_news():

    queries = [
        "gold XAUUSD Reuters",
        "Bitcoin cryptocurrency Reuters",
        "Federal Reserve Reuters",
        "US economy Reuters",
        "interest rates Reuters",
        "inflation Reuters",
        "oil Reuters",
        "geopolitics Reuters",
        "Middle East Reuters",
        "China Reuters",
        "European Central Bank Reuters",
        "financial markets Reuters"
    ]

    all_news = []

    for query in queries:
        news_items = google_news_rss(query)

        for item in news_items:
            all_news.append(item)

    # Remove duplicate links
    unique = {}

    for item in all_news:
        key = item["link"]

        if key not in unique:
            unique[key] = item

    return list(unique.values())


# ============================================================
# NEWS RELEVANCE / BREAKING CHECK
# ============================================================

def clean_text(text):
    text = re.sub("<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_recent_news(item):
    """
    Google News RSS timestamps can vary.
    We mainly use the feed's published time when available.
    """

    date_text = item.get("date", "")

    if not date_text:
        return True

    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(date_text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        age = utc_now() - dt.astimezone(timezone.utc)

        return age <= timedelta(hours=12)

    except Exception:
        return True


def ask_gemini_to_select_news(news_items, breaking=False):

    if not news_items:
        return None

    compact_items = []

    for index, item in enumerate(news_items[:40]):
        compact_items.append(
            f"""
ID: {index}
Source: {item["source"]}
Title: {item["title"]}
Description: {clean_text(item["description"])[:500]}
Link: {item["link"]}
"""
        )

    mode_text = (
        "BREAKING NEWS: Select only a genuinely important new development that could materially affect global markets, Gold, BTC, currencies, rates, commodities or major geopolitical risk."
        if breaking
        else
        "MARKET NEWS: Select one relevant and meaningful development for a Gold/BTC/global macro audience."
    )

    prompt = f"""
You are a strict financial-news editor.

{mode_text}

Candidate articles:

{"".join(compact_items)}

RULES:

1. Select ONLY one article.
2. Do not invent facts.
3. Do not combine facts from different articles.
4. Do not select rumors or social-media speculation.
5. Prefer Reuters, AP, Bloomberg, FT, BBC, CNBC or official institutions.
6. Preserve all important numbers, percentages, dates, names, countries and currencies exactly.
7. If an article says "may", do not change it to "will".
8. If an article says "reported", do not change it to "confirmed".
9. If an article says "rejected", do not change it to "considered".
10. Do not add predictions.
11. Do not add your own opinion.
12. For BREAKING NEWS, if none is genuinely important, return NONE.
13. For normal news, return NONE if there is no suitable article.

Return JSON only:

{{
  "selected_id": 0,
  "importance": "high",
  "reason": "short reason"
}}

OR:

{{
  "selected_id": null,
  "importance": "none",
  "reason": "No suitable article"
}}
"""

    try:
        response = client_ai.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=250,
                response_mime_type="application/json"
            )
        )

        return json.loads(response.text)

    except Exception as e:
        print(f"News selection error: {e}")
        return None


# ============================================================
# NEWS POST GENERATION
# ============================================================

def generate_news_post(item, breaking=False):

    headline_prefix = "🚨 BREAKING NEWS" if breaking else "📰 MARKET NEWS"

    prompt = f"""
Create a short professional Telegram news post.

Source:
{item["source"]}

Headline:
{item["title"]}

Description:
{clean_text(item["description"])}

Article link:
{item["link"]}

Time:
{utc_string()}

Rules:

1. Use ONLY facts in the supplied article information.
2. Do not invent anything.
3. Do not add opinions.
4. Do not predict market direction.
5. Preserve exact numbers, percentages, dates, names, countries and currencies.
6. Preserve uncertainty words such as may, could, reportedly, according to.
7. Do not change reported into confirmed.
8. Do not change rejected into considered.
9. Keep it to 2 or 3 short sentences.
10. Use simple professional English.
11. Do not copy the article.
12. Rewrite the information in original wording.
13. Include the source and link.
14. Use country flags only for countries explicitly mentioned.
15. No hashtags.
16. No Markdown bold.

Format:

{headline_prefix}

[Country flag if relevant] Short headline.

Short factual summary in 1–2 sentences.

Source: {item["source"]}
Link: {item["link"]}
Time: {utc_string()}
"""

    response = client_ai.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.15,
            max_output_tokens=450
        )
    )

    return response.text.strip()


# ============================================================
# NORMAL NEWS
# ============================================================

def post_normal_news():

    state = load_state()

    news_items = collect_news()

    news_items = [
        item
        for item in news_items
        if is_recent_news(item)
    ]

    # Remove already-posted news
    fresh_items = []

    for item in news_items:

        news_id = make_news_id(
            item["title"],
            item["link"]
        )

        if news_id not in state["news_ids"]:
            item["news_id"] = news_id
            fresh_items.append(item)

    if not fresh_items:
        print("No fresh news available.")
        return

    selected = ask_gemini_to_select_news(
        fresh_items,
        breaking=False
    )

    if not selected:
        print("No news selected.")
        return

    selected_id = selected.get("selected_id")

    if selected_id is None:
        print("No suitable news.")
        return

    if not isinstance(selected_id, int):
        print("Invalid selected news ID.")
        return

    if selected_id < 0 or selected_id >= len(fresh_items):
        print("Selected news ID out of range.")
        return

    item = fresh_items[selected_id]

    post = generate_news_post(
        item,
        breaking=False
    )

    send_to_telegram(post)

    state["news_ids"].append(
        item["news_id"]
    )

    state["news_ids"] = state["news_ids"][
        -MAX_STORED_NEWS_IDS:
    ]

    save_state(state)

    print("Normal news posted.")


# ============================================================
# BREAKING NEWS
# ============================================================

def post_breaking_news():

    state = load_state()

    state = reset_breaking_counter_if_new_day(
        state
    )

    save_state(state)

    if state["breaking_count"] >= MAX_BREAKING_PER_DAY:
        print(
            "Daily breaking-news limit reached."
        )
        return

    news_items = collect_news()

    news_items = [
        item
        for item in news_items
        if is_recent_news(item)
    ]

    fresh_items = []

    for item in news_items:

        news_id = make_news_id(
            item["title"],
            item["link"]
        )

        if news_id not in state["news_ids"]:
            item["news_id"] = news_id
            fresh_items.append(item)

    if not fresh_items:
        print("No fresh breaking-news candidates.")
        return

    selected = ask_gemini_to_select_news(
        fresh_items,
        breaking=True
    )

    if not selected:
        return

    selected_id = selected.get("selected_id")

    if selected_id is None:
        print("No genuine breaking news.")
        return

    if not isinstance(selected_id, int):
        return

    if selected_id < 0 or selected_id >= len(fresh_items):
        return

    item = fresh_items[selected_id]

    post = generate_news_post(
        item,
        breaking=True
    )

    send_to_telegram(post)

    state["breaking_count"] += 1

    state["news_ids"].append(
        item["news_id"]
    )

    state["news_ids"] = state["news_ids"][
        -MAX_STORED_NEWS_IDS:
    ]

    save_state(state)

    print(
        f"Breaking news posted. "
        f"Count today: {state['breaking_count']}"
    )


# ============================================================
# TEST
# ============================================================

def run_test():

    snapshot = get_market_snapshot()

    print("TEST MARKET SNAPSHOT:")
    print(snapshot)

    post = generate_market_post(snapshot)

    send_to_telegram(post)

    print("Test market post sent.")


# ============================================================
# MAIN
# ============================================================

def main():

    print("====================================")
    print("Gold Update & News 24/7 Bot")
    print(f"Mode: {POST_MODE}")
    print(f"UTC: {utc_string()}")
    print("====================================")

    if POST_MODE == "market":
        snapshot = get_market_snapshot()

        print("Market snapshot:")
        print(snapshot)

        post = generate_market_post(
            snapshot
        )

        send_to_telegram(post)

    elif POST_MODE == "news":
        post_normal_news()

    elif POST_MODE == "breaking":
        post_breaking_news()

    elif POST_MODE == "test":
        run_test()

    else:
        raise ValueError(
            "Invalid POST_MODE. "
            "Use: market, news, breaking or test."
        )


if __name__ == "__main__":
    main()
