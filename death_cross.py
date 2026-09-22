import os
import json
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SEEN_FILE = "seen.json"

NSE_URL = "https://www.nseindia.com/api/corporate-announcements"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}


# ============================================================
# INDIA DATE
# ============================================================

def get_today_india():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


# ============================================================
# LOAD SEEN ALERTS
# ============================================================

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(data)

        return set()

    except Exception as e:
        print(f"Could not read seen.json: {e}")
        return set()


# ============================================================
# SAVE SEEN ALERTS
# ============================================================

def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return False

    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID is missing.")
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

        response.raise_for_status()

        print("Telegram alert sent successfully.")
        return True

    except requests.RequestException as e:
        print(f"Telegram error: {e}")
        return False


# ============================================================
# NSE SESSION
# ============================================================

def create_nse_session():

    session = requests.Session()

    session.headers.update(HEADERS)

    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Host": "www.nseindia.com",
        "Referer": "https://www.nseindia.com/",
    })

    return session


# ============================================================
# GET TODAY'S NSE ANNOUNCEMENTS ONLY
# ============================================================

def get_announcements():

    session = create_nse_session()

    today = get_today_india()

    today_string = today.strftime("%d-%m-%Y")

    params = {
        "index": "equities",
        "from_date": today_string,
        "to_date": today_string,
    }

    print(f"Checking NSE announcements for: {today_string}")

    try:

        response = session.get(
            NSE_URL,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            return data.get("data", [])

        return []

    except requests.RequestException as e:
        print(f"NSE request failed: {e}")
        return []

    except ValueError as e:
        print(f"NSE returned invalid JSON: {e}")
        return []


# ============================================================
# IDENTIFY BONUS / STOCK SPLIT
# ============================================================

def identify_action(announcement):

    text_parts = []

    fields = [
        "desc",
        "description",
        "subject",
        "details",
        "headline",
        "attchmntText",
        "announcement",
        "purpose",
    ]

    for key in fields:

        value = announcement.get(key)

        if value:
            text_parts.append(str(value))

    text = " ".join(text_parts).lower()

    text = re.sub(r"\s+", " ", text)

    # BONUS
    bonus_patterns = [
        r"\bbonus\b",
        r"\bbonus issue\b",
        r"\bbonus shares\b",
        r"\bissue of bonus\b",
    ]

    for pattern in bonus_patterns:

        if re.search(pattern, text, re.IGNORECASE):
            return "BONUS"

    # STOCK SPLIT
    split_patterns = [
        r"\bstock split\b",
        r"\bshare split\b",
        r"\bface value\b.*\bsplit\b",
        r"\bsplit\b.*\bface value\b",
        r"\bsub-division\b",
        r"\bsub division\b",
        r"\bsubdivision\b",
    ]

    for pattern in split_patterns:

        if re.search(pattern, text, re.IGNORECASE):
            return "STOCK SPLIT"

    return None


# ============================================================
# UNIQUE ANNOUNCEMENT ID
# ============================================================

def announcement_id(announcement):

    # Prefer NSE's own identifiers
    for key in [
        "seq_id",
        "seqId",
        "id",
    ]:

        value = announcement.get(key)

        if value:
            return f"{key}:{value}"

    # Build a stable fallback ID
    symbol = str(
        announcement.get("symbol")
        or announcement.get("symbolName")
        or ""
    )

    description = str(
        announcement.get("desc")
        or announcement.get("description")
        or announcement.get("subject")
        or ""
    )

    broadcast_date = str(
        announcement.get("broadcastDate")
        or announcement.get("date")
        or ""
    )

    return (
        f"{symbol}|"
        f"{broadcast_date}|"
        f"{description.strip().lower()}"
    )


# ============================================================
# FORMAT TELEGRAM MESSAGE
# ============================================================

def format_message(action, announcement):

    symbol = (
        announcement.get("symbol")
        or announcement.get("symbolName")
        or "Unknown"
    )

    company = (
        announcement.get("companyName")
        or announcement.get("symbol")
        or "Unknown company"
    )

    description = (
        announcement.get("desc")
        or announcement.get("description")
        or announcement.get("subject")
        or announcement.get("details")
        or announcement.get("headline")
        or "Corporate action announcement"
    )

    date_value = (
        announcement.get("broadcastDate")
        or announcement.get("date")
        or announcement.get("sort_date")
        or ""
    )

    attachment = (
        announcement.get("attchmntFile")
        or announcement.get("attachment")
        or announcement.get("url")
        or ""
    )

    message = (
        f"🚨 FRESH {action} ALERT\n\n"
        f"🏢 Company: {company}\n"
        f"📌 Symbol: {symbol}\n"
        f"📅 Announcement Date: {date_value}\n\n"
        f"📝 Details:\n{description}"
    )

    if attachment:

        attachment = str(attachment)

        if attachment.startswith("http"):
            message += f"\n\n🔗 {attachment}"

        else:
            message += (
                "\n\n🔗 https://www.nseindia.com/"
                + attachment.lstrip("/")
            )

    message += "\n\n🤖 NSE Corporate Action Scanner"

    return message


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("NSE FRESH BONUS & STOCK SPLIT SCANNER")
    print("=" * 60)

    today = get_today_india()

    print(f"India date: {today}")

    seen = load_seen()

    print(f"Previously alerted: {len(seen)}")

    announcements = get_announcements()

    print(f"Announcements received from NSE: {len(announcements)}")

    if not announcements:

        print("No NSE announcements found for today.")
        save_seen(seen)
        return

    new_alerts = 0

    for announcement in announcements:

        action = identify_action(announcement)

        if action is None:
            continue

        ann_id = announcement_id(announcement)

        print()
        print(f"Detected: {action}")
        print(f"ID: {ann_id}")

        # ====================================================
        # DUPLICATE CHECK
        # ====================================================

        if ann_id in seen:

            print("Already alerted. SKIPPING.")

            continue

        # ====================================================
        # NEW ALERT
        # ====================================================

        message = format_message(
            action,
            announcement
        )

        print("NEW FRESH ALERT:")
        print(message)

        sent = send_telegram(message)

        if sent:

            seen.add(ann_id)

            new_alerts += 1

            print("Added to seen.json.")

        else:

            print("Telegram failed. NOT adding to seen.json.")

    # ========================================================
    # SAVE SEEN IDS
    # ========================================================

    save_seen(seen)

    print()
    print("=" * 60)
    print(f"NEW ALERTS SENT: {new_alerts}")
    print(f"TOTAL SEEN ALERTS: {len(seen)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
