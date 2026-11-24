
"""Combined Golden Cross + Death Cross scanner.
Daily NSE EQ stocks | EMA 50/200 | completed candles only.
Publishes fresh signals to Telegram and Share Sanket.
"""

import base64
import datetime as dt
import html
import io
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf


# ---------------- CONFIGURATION ----------------

FAST = int(os.getenv("FAST_MA", "50"))
SLOW = int(os.getenv("SLOW_MA", "200"))
MA_TYPE = os.getenv("MA_TYPE", "EMA").upper()

FRESH_BARS = int(os.getenv("FRESH_BARS", "1"))
INCLUDE_LIVE_BAR = os.getenv("INCLUDE_LIVE_BAR", "false").lower() == "true"

ENABLE_GOLDEN = os.getenv("ENABLE_GOLDEN", "true").lower() == "true"
ENABLE_DEATH = os.getenv("ENABLE_DEATH", "true").lower() == "true"

NSE_ALL = os.getenv("NSE_ALL", "true").lower() == "true"
NSE_SERIES = {
    item.strip()
    for item in os.getenv("NSE_SERIES", "EQ").split(",")
    if item.strip()
}

MIN_AVG_VOLUME = int(os.getenv("MIN_AVG_VOLUME", "0"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "150"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SHARE_SANKET_TOKEN = os.getenv("SHARE_SANKET_TOKEN", "")
SHARE_SANKET_REPO = os.getenv(
    "SHARE_SANKET_REPO", "sharesanket/corporate-actions.yml"
)
SHARE_SANKET_BRANCH = os.getenv("SHARE_SANKET_BRANCH", "main")

IST = ZoneInfo("Asia/Kolkata")

STATE_FILE = Path("state.json")
CACHE_FILE = Path("nse_equities.txt")
EXTRA_FILE = Path("symbols.txt")

NSE_URL = (
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
)

HEADERS = {
    "golden": "🟡 <b>GOLDEN CROSS (Daily)</b>",
    "death": "🔴 <b>DEATH CROSS (Daily)</b>",
}


# ---------------- NSE STOCK UNIVERSE ----------------

def get_nse_symbols():
    try:
        response = requests.get(
            NSE_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/csv,*/*",
            },
            timeout=30,
        )
        response.raise_for_status()

        df = pd.read_csv(io.StringIO(response.text))
        df.columns = [str(col).strip() for col in df.columns]

        df["SERIES"] = df["SERIES"].astype(str).str.strip()
        symbols = sorted(
            df.loc[
                df["SERIES"].isin(NSE_SERIES), "SYMBOL"
            ].astype(str).str.strip().unique()
        )

        if len(symbols) > 500:
            cache_text = "\n".join(symbols) + "\n"
            if (
                not CACHE_FILE.exists()
                or CACHE_FILE.read_text(encoding="utf-8") != cache_text
            ):
                CACHE_FILE.write_text(cache_text, encoding="utf-8")
            print(f"[NSE] Loaded {len(symbols)} EQ symbols")
            return symbols

        print(f"[NSE warning] Only {len(symbols)} symbols received")

    except Exception as exc:
        print(f"[NSE warning] Download failed: {exc}")

    if CACHE_FILE.exists():
        cached = CACHE_FILE.read_text(encoding="utf-8").split()
        print(f"[NSE] Using cached list: {len(cached)} symbols")
        return cached

    raise RuntimeError(
        "NSE symbol list unavailable and no cache exists."
    )


def load_universe():
    tickers = []

    if NSE_ALL:
        tickers.extend(
            f"{symbol}.NS" for symbol in get_nse_symbols()
        )

    if EXTRA_FILE.exists():
        for line in EXTRA_FILE.read_text(encoding="utf-8").splitlines():
            ticker = line.split("#")[0].strip()
            if ticker:
                tickers.append(ticker)

    return list(dict.fromkeys(tickers))


# ---------------- MOVING AVERAGES ----------------

def moving_average(series, period):
    if MA_TYPE == "EMA":
        return series.ewm(
            span=period, adjust=False, min_periods=period
        ).mean()

    if MA_TYPE == "SMA":
        return series.rolling(
            window=period, min_periods=period
        ).mean()

    raise ValueError("MA_TYPE must be EMA or SMA")


def is_today_ist(index_value):
    timestamp = pd.Timestamp(index_value)

    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(IST)

    return timestamp.date() == dt.datetime.now(IST).date()


def detect_cross(close, volume=None):
    result = {"golden": None, "death": None}

    close = pd.to_numeric(close, errors="coerce").dropna()
    if close.empty:
        return result

    # Exclude today's still-forming daily candle.
    if not INCLUDE_LIVE_BAR and is_today_ist(close.index[-1]):
        close = close.iloc[:-1]

    if len(close) < SLOW + 2:
        return result

    fast = moving_average(close, FAST)
    slow = moving_average(close, SLOW)
    difference = fast - slow

    # Genuine crossover: previous relationship must be opposite.
    golden_cross = (
        (difference.shift(1) <= 0) & (difference > 0)
    )
    death_cross = (
        (difference.shift(1) >= 0) & (difference < 0)
    )

    if MIN_AVG_VOLUME > 0 and volume is not None:
        average_volume = pd.to_numeric(
            volume.reindex(close.index), errors="coerce"
        ).tail(20).mean()

        if pd.isna(average_volume) or average_volume < MIN_AVG_VOLUME:
            return result

    checks = [
        ("golden", golden_cross, ENABLE_GOLDEN),
        ("death", death_cross, ENABLE_DEATH),
    ]

    for kind, signal_series, enabled in checks:
        if not enabled:
            continue

        recent = signal_series.tail(FRESH_BARS)
        if not recent.any():
            continue

        signal_date = recent[recent].index[-1]

        result[kind] = {
            "date": pd.Timestamp(signal_date).strftime("%Y-%m-%d"),
            "close": float(close.loc[signal_date]),
            "fast": float(fast.loc[signal_date]),
            "slow": float(slow.loc[signal_date]),
        }

    return result


# ---------------- MARKET DATA ----------------

def download_batch(tickers):
    for attempt in range(3):
        try:
            data = yf.download(
                tickers,
                period="3y",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )

            if data is not None and not data.empty:
                return data

            print("[Yahoo warning] Empty data returned")

        except Exception as exc:
            print(f"[Yahoo retry {attempt + 1}] {exc}")

        time.sleep(3 * (attempt + 1))

    return None


def get_ticker_frame(data, ticker, batch):
    if not isinstance(data.columns, pd.MultiIndex):
        return data if len(batch) == 1 else None

    level_zero = data.columns.get_level_values(0)
    level_one = data.columns.get_level_values(1)

    if ticker in level_zero:
        return data[ticker]

    if ticker in level_one:
        return data.xs(ticker, axis=1, level=1)

    return None


def scan(tickers):
    hits = []

    for start in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[start:start + BATCH_SIZE]
        batch_number = start // BATCH_SIZE + 1

        print(
            f"[Scan] Batch {batch_number}: "
            f"{len(batch)} stocks"
        )

        data = download_batch(batch)

        if data is None or data.empty:
            print(f"[Scan warning] Batch {batch_number} skipped")
            continue

        for ticker in batch:
            frame = get_ticker_frame(data, ticker, batch)

            if frame is None or "Close" not in frame.columns:
                continue

            try:
                signals = detect_cross(
                    frame["Close"],
                    frame["Volume"] if "Volume" in frame else None,
                )

                for kind, signal in signals.items():
                    if signal:
                        hits.append((ticker, kind, signal))

            except Exception as exc:
                print(f"[Stock error] {ticker}: {exc}")

        print(
            f"[Scan] Completed batch {batch_number}; "
            f"{len(hits)} signals found so far"
        )

        time.sleep(1)

    return hits


# ---------------- STATE / DUPLICATE CONTROL ----------------

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("State file must contain a JSON object")
        return state

    except Exception as exc:
        raise RuntimeError(
            f"Cannot safely read state.json: {exc}"
        )


def save_state(state):
    new_text = json.dumps(
        state, indent=2, sort_keys=True
    ) + "\n"

    STATE_FILE.write_text(new_text, encoding="utf-8")


def state_key(ticker, kind):
    # Preserve the existing state-key format.
    return ticker if kind == "golden" else f"{ticker}|death"


def find_fresh_hits(hits, state):
    fresh = []

    for ticker, kind, signal in hits:
        key = state_key(ticker, kind)
        previous_date = state.get(key, "")

        if signal["date"] > previous_date:
            fresh.append((ticker, kind, signal))

    return fresh


# ---------------- TELEGRAM ----------------

def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def format_telegram_line(ticker, kind, signal):
    symbol = ticker.removesuffix(".NS")

    if ticker.endswith(".NS"):
        url = (
            "https://www.tradingview.com/chart/?symbol=NSE:"
            + quote(symbol)
        )
        label = f'<a href="{url}">{html.escape(symbol)}</a>'
    else:
        label = html.escape(ticker)

    comparison = ">" if kind == "golden" else "<"

    return (
        f"• <b>{label}</b> ₹{signal['close']:.2f} | "
        f"{MA_TYPE}{FAST}: {signal['fast']:.2f} "
        f"{comparison} {MA_TYPE}{SLOW}: {signal['slow']:.2f} | "
        f"{signal['date']}"
    )


def send_fresh_telegram(fresh, state):
    for kind in ("golden", "death"):
        items = sorted(
            [
                (ticker, signal)
                for ticker, signal_kind, signal in fresh
                if signal_kind == kind
            ],
            key=lambda item: item[0],
        )

        for start in range(0, len(items), 15):
            chunk = items[start:start + 15]

            message = (
                f"{HEADERS[kind]} — {len(items)} new signal(s)\n"
                + "\n".join(
                    format_telegram_line(ticker, kind, signal)
                    for ticker, signal in chunk
                )
            )

            # Do not mark these signals as sent if Telegram fails.
            send_telegram(message)

            for ticker, signal in chunk:
                state[state_key(ticker, kind)] = signal["date"]

            save_state(state)
            print(
                f"[Telegram] Sent {len(chunk)} {kind} signal(s)"
            )
            time.sleep(1)


# ---------------- SHARE SANKET PUBLISHER ----------------

def publish_to_sharesanket(fresh):
    if not SHARE_SANKET_TOKEN:
        raise RuntimeError(
            "SHARE_SANKET_TOKEN is missing. "
            "Configure it in GitHub repository secrets."
        )

    if not fresh:
        print("[Share Sanket] No fresh signals to publish")
        return

    api_url = (
        f"https://api.github.com/repos/{SHARE_SANKET_REPO}"
        "/contents/alerts.json"
    )

    headers = {
        "Authorization": f"Bearer {SHARE_SANKET_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    now = dt.datetime.now(IST).isoformat()

    new_alerts = []

    for ticker, kind, signal in fresh:
        symbol = ticker.removesuffix(".NS")
        signal_id = (
            f"ema-{kind}-{symbol}-{signal['date']}"
        ).lower()

        tradingview_url = (
            "https://www.tradingview.com/chart/?symbol=NSE:"
            + quote(symbol)
        )

        is_golden = kind == "golden"

        new_alerts.append({
            "id": signal_id,
            "time": now,
            "folder": "emas",
            "subfolder": kind,
            "symbol": symbol,
            "company": symbol,
            "title": (
                "Golden Cross (Daily)"
                if is_golden else "Death Cross (Daily)"
            ),
            "price": round(signal["close"], 2),
            "change_pct": None,
            "details": [
                {
                    "label": "Signal date",
                    "value": signal["date"],
                    "source": "Yahoo Finance",
                },
                {
                    "label": f"{MA_TYPE}{FAST}",
                    "value": round(signal["fast"], 4),
                    "source": "Yahoo Finance",
                },
                {
                    "label": f"{MA_TYPE}{SLOW}",
                    "value": round(signal["slow"], 4),
                    "source": "Yahoo Finance",
                },
                {
                    "label": "Timeframe",
                    "value": "Daily",
                    "source": "Scanner",
                },
                {
                    "label": "Signal definition",
                    "value": (
                        f"{MA_TYPE}{FAST} "
                        f"{'crossed above' if is_golden else 'crossed below'} "
                        f"{MA_TYPE}{SLOW}"
                    ),
                    "source": "Scanner",
                },
            ],
            "link": tradingview_url,
        })

    # Retry if another Share Sanket process updates alerts.json
    # between our GET and PUT requests.
    for attempt in range(3):
        try:
            response = requests.get(
                api_url,
                headers=headers,
                params={"ref": SHARE_SANKET_BRANCH},
                timeout=30,
            )
            response.raise_for_status()

            remote = response.json()
            data = json.loads(
                base64.b64decode(remote["content"]).decode("utf-8")
            )

            if not isinstance(data.get("alerts"), list):
                raise ValueError(
                    "Share Sanket alerts.json has no alerts list"
                )

            existing_ids = {
                str(item.get("id"))
                for item in data["alerts"]
            }

            additions = [
                alert for alert in new_alerts
                if alert["id"] not in existing_ids
            ]

            if not additions:
                print(
                    "[Share Sanket] Signals already exist; no duplicates"
                )
                return

            data["alerts"] = additions + data["alerts"]
            data["updated"] = now

            encoded = base64.b64encode(
                json.dumps(
                    data, ensure_ascii=False, indent=2
                ).encode("utf-8")
            ).decode("ascii")

            saved = requests.put(
                api_url,
                headers=headers,
                json={
                    "message": (
                        f"Publish {len(additions)} fresh EMA cross alert(s)"
                    ),
                    "content": encoded,
                    "sha": remote["sha"],
                    "branch": SHARE_SANKET_BRANCH,
                },
                timeout=30,
            )

            if saved.status_code in (409, 422):
                if attempt < 2:
                    print("[Share Sanket] File changed; retrying")
                    time.sleep(2 * (attempt + 1))
                    continue

            saved.raise_for_status()

            print(
                f"[Share Sanket] Published {len(additions)} signal(s)"
            )
            return

        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(
                    f"Share Sanket publishing failed: {exc}"
                ) from exc

            print(
                f"[Share Sanket] Retry {attempt + 1}: {exc}"
            )
            time.sleep(2 * (attempt + 1))


# ---------------- MAIN ----------------

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required"
        )

    if FAST >= SLOW:
        raise ValueError("FAST_MA must be smaller than SLOW_MA")

    if FRESH_BARS < 1:
        raise ValueError("FRESH_BARS must be at least 1")

    if "--test" in sys.argv:
        send_telegram(
            "✅ Combined Golden Cross + Death Cross bot connected."
        )
        print("[Test] Telegram connection successful")
        return

    print("=" * 55)
    print("COMBINED GOLDEN + DEATH CROSS SCANNER")
    print(f"MA: {MA_TYPE}{FAST} / {MA_TYPE}{SLOW}")
    print("Timeframe: Daily")
    print("Completed candles only:", not INCLUDE_LIVE_BAR)
    print("Golden enabled:", ENABLE_GOLDEN)
    print("Death enabled:", ENABLE_DEATH)
    print("Started IST:", dt.datetime.now(IST).isoformat())
    print("=" * 55)

    universe = load_universe()
    print(f"[Universe] {len(universe)} tickers")

    state = load_state()
    hits = scan(universe)

    fresh = find_fresh_hits(hits, state)

    print(f"[Result] Total recent crosses: {len(hits)}")
    print(f"[Result] Fresh signals: {len(fresh)}")

    if not fresh:
        print("[Result] No fresh signals. Finished.")
        return

    # Publish to the app first. If this fails, stop and retry next run.
    # Stable IDs prevent duplicate app entries on a retry.
    publish_to_sharesanket(fresh)

    # Telegram failures do not mark failed messages as sent.
    send_fresh_telegram(fresh, state)

    print("[Complete] Combined scan finished successfully")
    print("Finished IST:", dt.datetime.now(IST).isoformat())


if __name__ == "__main__":
    main()
