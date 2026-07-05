"""Entry point for the bazos monitor bot.

Behavior:
- Load ENV
- Fetch postmarketOS-supported device list
- Poll bazos category page (RSS preferred) and search for device keywords + supported models
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
BAZOS_SEARCH_URL = os.getenv("BAZOS_SEARCH_URL", "https://www.bazos.sk/rss.php?rub=mo&cat=451")
TARGET_KEYWORDS = os.getenv("TARGET_KEYWORDS", "").split(",")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
PRICE_CAP_EUR = float(os.getenv("PRICE_CAP_EUR", "50"))  # strict cap; entries without parseable price are excluded



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
    pm_keywords = [t.lower() for t in pm]
    kws = [k.strip().lower() for k in TARGET_KEYWORDS if k.strip()]
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
    parts = [f"Title: {item.get('title')}", f"URL: {item.get('url')}"]
    if item.get('price'):
        parts.append(f"Price: {item.get('price')}")
    if item.get('published'):
        parts.append(f"Published: {item.get('published')}")
    return "\n".join(parts)


def main_loop():
    seen = load_seen()
    while True:
        keywords = build_search_keywords()
        if not keywords:
            print("No keywords configured; exiting.")
            return
        matches = search_listings(BAZOS_SEARCH_URL, keywords)
        for m in matches:
            # Apply strict price cap: require parseable price and value <= PRICE_CAP_EUR
            price_eur = m.get('price_eur')
            if price_eur is None:
                # skip items without price when using strict cap
                continue
            try:
                if float(price_eur) > PRICE_CAP_EUR:
                    continue
            except Exception:
                continue

            uid = m.get('url') or ''
            if not uid:
                # fallback to title-based UID
                uid = (m.get('title') or '')[:200]
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
