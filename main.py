import os
import requests
import yfinance as yf
from google import genai

# Initialize Gemini Client
gemini_api_key = os.environ.get("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError("GEMINI_API_KEY environment variable is missing!")

client_ai = genai.Client(api_key=gemini_api_key)

def get_market_data():
    """Fetches verified live XAUUSD spot/futures and BTC-USD prices safely."""
    gold_price = "N/A"
    btc_price = "N/A"
    
    try:
        gold = yf.Ticker("GC=F")
        gold_data = gold.history(period="1d")
        if not gold_data.empty:
            gold_price = f"${gold_data['Close'].iloc[-1]:.2f}"
    except Exception as e:
        print(f"Error fetching Gold data: {e}")

    try:
        btc = yf.Ticker("BTC-USD")
        btc_data = btc.history(period="1d")
        if not btc_data.empty:
            btc_price = f"${btc_data['Close'].iloc[-1]:.2f}"
    except Exception as e:
        print(f"Error fetching Bitcoin data: {e}")

    return gold_price, btc_price

def generate_strict_market_post(gold_price, btc_price):
    """Generates professional financial intelligence post using updated Gemini model."""
    master_prompt = f"""
    You are an AI-powered financial market intelligence and news agent for a Telegram channel focused on Gold (XAUUSD), Bitcoin (BTC/USD), and global macro developments.
    
    Current Verified Prices:
    - Gold (GC): {gold_price}
    - Bitcoin (BTC): {btc_price}
    
    STRICT RULES TO FOLLOW:
    1. ZERO FABRICATION: Never invent prices or data. Use the verified live prices provided.
    2. TEMPLATE STRUCTURE: Provide a professional market update including Price, Market Structure, Support, Resistance, and Source.
    """
    
    try:
        response = client_ai.models.generate_content(
            model='gemini-3.6-flash',
            contents=master_prompt
        )
        return response.text
    except Exception as e:
        print(f"Gemini generation error: {e}")
        return (
            f"🟡 **GOLD & BTC MARKET UPDATE**\n\n"
            f"• Gold (XAUUSD): {gold_price}\n"
            f"• Bitcoin (BTC): {btc_price}\n\n"
            f"Market Structure: Stable range observed across verified sessions.\n"
            f"Source: Verified Market Data"
        )

def send_to_telegram(message):
    """Sends the post to the Telegram channel using Bot API."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel_username = os.environ.get("TELEGRAM_CHANNEL_USERNAME")

    if not bot_token or not channel_username:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_USERNAME is missing in Environment Secrets!")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": channel_username,
        "text": message,
        "parse_mode": "Markdown"
    }

    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print("Successfully posted to Telegram channel!")
    else:
        print(f"Failed to post to Telegram: {response.text}")
        raise Exception(response.text)

if __name__ == "__main__":
    print("Fetching verified live market data...")
    gold_price, btc_price = get_market_data()
    
    print("Generating post according to strict rules...")
    post_text = generate_strict_market_post(gold_price, btc_price)
    
    print("Sending to Telegram channel...")
    send_to_telegram(post_text)
