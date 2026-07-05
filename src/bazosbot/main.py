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
from .evaluator import evaluate_listing


import logging

load_dotenv()

# logging config
LOG_LEVEL = os.getenv('DEBUG', '').lower() in ('1', 'true', 'yes') and logging.DEBUG or logging.INFO
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s [%(module)s:%(funcName)s] %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
SEEN_FILE = DATA_DIR / "seen.json"
DATA_DIR.mkdir(exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BAZOS_SEARCH_URL = os.getenv("BAZOS_SEARCH_URL", "https://www.bazos.sk/rss.php?rub=mo&cat=451")
# support multiple search URLs via comma-separated env var; fall back to single BAZOS_SEARCH_URL
BAZOS_SEARCH_URLS = [u.strip() for u in os.getenv("BAZOS_SEARCH_URLS", BAZOS_SEARCH_URL).split(",") if u.strip()]
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
    # use postmarketOS-supported models as search keywords
    pm = get_supported_models()
    pm_keywords = [t.lower() for t in pm]
    # dedupe
    seen = set()
    out = []
    for k in pm_keywords:
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
            logger.info("No keywords configured; exiting.")
            return
        # also pass postmarketOS model list to scraper for matching
        supported_models = get_supported_models()
        logger.debug("search keywords count=%d models=%d urls=%d", len(keywords), len(supported_models), len(BAZOS_SEARCH_URLS))
        all_matches = []
        for url in BAZOS_SEARCH_URLS:
            logger.debug("Searching URL: %s", url)
            try:
                matches = search_listings(url, keywords, supported_models=list(supported_models))
            except Exception as ex:
                logger.exception("search_listings failed for %s: %s", url, ex)
                matches = []
            for m in matches:
                # annotate match with source URL
                m['source_search_url'] = url
            all_matches.extend(matches)
        logger.debug("raw matches found=%d", len(all_matches))
        for m in all_matches:
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
                logger.debug("skipping seen uid=%s", uid)
                continue

            # additional confirmation: require strong match for keyword matches to avoid false positives
            matched_by = m.get('matched_by')
            match_type = m.get('match_type')
            if match_type == 'keyword' and matched_by:
                # use scraper strong_match
                from .scraper import strong_match
                if not strong_match(m.get('title') or '', matched_by):
                    logger.debug("rejected fuzzy-only match for uid=%s matched_by=%s title=%s", uid, matched_by, (m.get('title') or '')[:120])
                    continue

            # run evaluation
            supported = get_supported_models()
            eval_res = evaluate_listing({
                'title': m.get('title'),
                'url': m.get('url'),
                'price': m.get('price'),
                'price_eur': m.get('price_eur'),
                'published': m.get('published'),
            }, supported)
            logger.info("Evaluation result: postmarketos=%s confidence=%.2f k3s=%s ai=%s", eval_res.get('postmarketos_support'), eval_res.get('support_confidence'), eval_res.get('k3s_suitability'), eval_res.get('ai_used'))
            logger.debug("evaluation reasons=%s", eval_res.get('reasons'))

            # attach evaluation to message
            msg = format_message(m) + "\n\nEvaluation:\n" + "\n".join([f"- {r}" for r in eval_res.get('reasons', [])])
            logger.info("New match: %s", uid)
            logger.debug("message=\n%s", msg)
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                ok = send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
                logger.info("telegram send status=%s", ok)
        save_seen(seen)
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main_loop()
