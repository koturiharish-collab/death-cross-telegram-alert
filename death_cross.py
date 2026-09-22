import os
import json
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SEEN_FILE = "seen.json"

NSE_EQUITY_URL = (
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
)

NSE_HOME = "https://www.nseindia.com/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# SEEN STATE
# ============================================================

def load_seen():

    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(data)

    except Exception as e:
        print(f"Could not read seen.json: {e}")

    return set()


def save_seen(seen):

    try:

        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(
                sorted(seen),
                f,
                indent=2
            )

        print(f"Saved {len(seen)} alert states.")

    except Exception as e:
        print(f"Could not save seen.json: {e}")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN missing")
        return False

    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID missing")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=30
        )

        if response.status_code == 200:

            print("Telegram alert sent.")
            return True

        print(
            "Telegram error:",
            response.status_code,
            response.text
        )

    except Exception as e:

        print(f"Telegram request failed: {e}")

    return False


# ============================================================
# NSE SYMBOL LIST
# ============================================================

def get_nse_symbols():

    print("Downloading NSE equity list...")

    try:

        session = requests.Session()

        session.headers.update(HEADERS)

        # First visit NSE
        try:
            session.get(
                NSE_HOME,
                timeout=20
            )
        except Exception:
            pass

        response = session.get(
            NSE_EQUITY_URL,
            timeout=30
        )

        response.raise_for_status()

        from io import StringIO

        df = pd.read_csv(
            StringIO(response.text)
        )

        if "SYMBOL" not in df.columns:

            print(
                "SYMBOL column not found."
            )

            return []

        symbols = (
            df["SYMBOL"]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
            .tolist()
        )

        # Remove obvious invalid entries
        symbols = [
            s for s in symbols
            if s
            and s.upper() != "SYMBOL"
            and " " not in s
        ]

        print(
            f"NSE symbols found: {len(symbols)}"
        )

        return symbols

    except Exception as e:

        print(
            f"Could not download NSE symbols: {e}"
        )

        return []


# ============================================================
# GET DAILY DATA
# ============================================================

def get_stock_data(symbol):

    ticker = f"{symbol}.NS"

    try:

        data = yf.download(
            ticker,
            period="1y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
            timeout=20
        )

        if data is None or data.empty:
            return None

        # yfinance can return MultiIndex columns
        if isinstance(data.columns, pd.MultiIndex):

            try:
                close = data["Close"][ticker]
            except Exception:

                try:
                    close = data["Close"].iloc[:, 0]
                except Exception:
                    return None

        else:

            if "Close" not in data.columns:
                return None

            close = data["Close"]

        close = pd.to_numeric(
            close,
            errors="coerce"
        ).dropna()

        if len(close) < 210:
            return None

        return close

    except Exception as e:

        print(
            f"{symbol}: data error - {e}"
        )

        return None


# ============================================================
# CHECK FRESH DEATH CROSS
# ============================================================

def check_death_cross(close):

    ema50 = close.ewm(
        span=50,
        adjust=False
    ).mean()

    ema200 = close.ewm(
        span=200,
        adjust=False
    ).mean()

    if len(ema50) < 2 or len(ema200) < 2:
        return None

    previous_ema50 = float(
        ema50.iloc[-2]
    )

    previous_ema200 = float(
        ema200.iloc[-2]
    )

    current_ema50 = float(
        ema50.iloc[-1]
    )

    current_ema200 = float(
        ema200.iloc[-1]
    )

    # ========================================================
    # THIS IS THE IMPORTANT PART
    #
    # Previous:
    # 50 EMA >= 200 EMA
    #
    # Current:
    # 50 EMA < 200 EMA
    #
    # Therefore ONLY the actual crossover is alerted.
    # ========================================================

    fresh_cross = (
        previous_ema50 >= previous_ema200
        and
        current_ema50 < current_ema200
    )

    if not fresh_cross:
        return None

    latest_close = float(
        close.iloc[-1]
    )

    latest_date = close.index[-1]

    try:
        date_string = latest_date.strftime(
            "%Y-%m-%d"
        )
    except Exception:
        date_string = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")

    return {
        "date": date_string,
        "close": latest_close,
        "ema50": current_ema50,
        "ema200": current_ema200,
    }


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def make_message(
    symbol,
    result
):

    return (
        "🔴 FRESH DEATH CROSS\n\n"
        f"📊 Stock: {symbol}\n"
        f"📅 Cross Date: {result['date']}\n"
        f"💰 Close: ₹{result['close']:.2f}\n"
        f"📉 50 EMA: ₹{result['ema50']:.2f}\n"
        f"📈 200 EMA: ₹{result['ema200']:.2f}\n\n"
        "⚠️ 50 EMA crossed BELOW 200 EMA\n\n"
        "🤖 NSE Death Cross Scanner"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 65)
    print("NSE FRESH DEATH CROSS SCANNER")
    print("=" * 65)

    seen = load_seen()

    print(
        f"Previously saved alerts: {len(seen)}"
    )

    symbols = get_nse_symbols()

    if not symbols:

        print(
            "No NSE symbols found."
        )

        return

    fresh_count = 0
    checked_count = 0

    for index, symbol in enumerate(
        symbols,
        start=1
    ):

        print(
            f"[{index}/{len(symbols)}] "
            f"Checking {symbol}"
        )

        close = get_stock_data(
            symbol
        )

        if close is None:
            continue

        checked_count += 1

        result = check_death_cross(
            close
        )

        # ----------------------------------------------------
        # NO FRESH CROSS
        # ----------------------------------------------------

        if result is None:

            continue

        # ----------------------------------------------------
        # UNIQUE EVENT
        # ----------------------------------------------------

        event_key = (
            f"{symbol}|"
            f"DEATH_CROSS|"
            f"{result['date']}"
        )

        # ----------------------------------------------------
        # ALREADY SENT
        # ----------------------------------------------------

        if event_key in seen:

            print(
                f"{symbol}: already alerted"
            )

            continue

        # ----------------------------------------------------
        # FRESH CROSS
        # ----------------------------------------------------

        message = make_message(
            symbol,
            result
        )

        print()
        print("🔥 FRESH DEATH CROSS FOUND")
        print(message)
        print()

        success = send_telegram(
            message
        )

        if success:

            seen.add(
                event_key
            )

            fresh_count += 1

            # Save immediately
            # so an interruption does not
            # cause the same alert again.
            save_seen(seen)

        # Small delay
        time.sleep(0.15)

    # Final save
    save_seen(seen)

    print("=" * 65)
    print(
        f"Stocks checked: {checked_count}"
    )
    print(
        f"Fresh death crosses: {fresh_count}"
    )
    print("=" * 65)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
