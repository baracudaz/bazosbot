"""Entry point for the bazos monitor bot.

Behavior:
- Load ENV
- Fetch postmarketOS-supported device list
- Poll bazos category page and search for device keywords + supported models
- Log matches to stdout and send Telegram message if configured
"""
import os
import time
import json
from dotenv import load_dotenv
from pathlib import Path

from .postmarketos import get_supported_models
from .scraper import search_listings
from .notifier import send_telegram


load_dotenv()
DATA_DIR = Path("data")
SEEN_FILE = DATA_DIR / "seen.json"
DATA_DIR.mkdir(exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BAZOS_SEARCH_URL = os.getenv("BAZOS_SEARCH_URL", "https://www.bazos.sk/telefony/")
TARGET_KEYWORDS = os.getenv("TARGET_KEYWORDS", "").split(",")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(s):
    SEEN_FILE.write_text(json.dumps(list(s)))


def build_search_keywords() -> list:
    # combine explicit keywords with postmarketOS-supported models
    pm = get_supported_models()
    # pm contains titles like 'Xiaomi Mi 9T' — normalize to lower
    pm_keywords = [t.lower() for t in pm]
    kws = [k.strip().lower() for k in TARGET_KEYWORDS if k.strip()]
    # include pm keywords that are short enough to match comfortably
    combined = kws + pm_keywords
    # dedupe and limit
    seen = set()
    out = []
    for k in combined:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def format_message(item):
    return f"Found: {item.get('title')[:200]}\n{item.get('url')}"


def main_loop():
    seen = load_seen()
    while True:
        keywords = build_search_keywords()
        if not keywords:
            print("No keywords configured; exiting.")
            return
        matches = search_listings(BAZOS_SEARCH_URL, keywords)
        for m in matches:
            uid = m.get('url') + '|' + (m.get('title') or '')
            if uid in seen:
                continue
            seen.add(uid)
            msg = format_message(m)
            print(msg)
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
        save_seen(seen)
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main_loop()
