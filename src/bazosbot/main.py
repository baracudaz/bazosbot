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
import re
from dotenv import load_dotenv
from pathlib import Path

from .postmarketos import get_supported_models
from .scraper import search_listings, enrich_listing_price, strong_match, _contains_czk_currency
from .notifier import send_telegram
from .evaluator import evaluate_listing


import logging

load_dotenv()

# logging config
def _resolve_log_level() -> int:
    if configured := os.getenv("LOG_LEVEL", "").strip().upper():
        level = getattr(logging, configured, None)
        if isinstance(level, int):
            return level
    # Backward compatibility for older DEBUG=true style config.
    if os.getenv("DEBUG", "").lower() in ("1", "true", "yes"):
        return logging.DEBUG
    return logging.INFO


logging.basicConfig(level=_resolve_log_level(), format='%(asctime)s %(levelname)s [%(module)s:%(funcName)s] %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
SEEN_FILE = DATA_DIR / "seen.json"
DATA_DIR.mkdir(exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEFAULT_BAZOS_SEARCH_URLS = [
    "https://www.bazos.sk/rss.php?rub=mo&cat=451",
    "https://www.bazos.sk/rss.php?rub=mo&cat=436",
    "https://www.bazos.sk/rss.php?rub=mo&cat=304",
    "https://www.bazos.sk/rss.php?rub=mo&cat=307",
    "https://www.bazos.cz/rss.php?rub=mo&cat=455",
    "https://www.bazos.cz/rss.php?rub=mo&cat=440",
    "https://www.bazos.cz/rss.php?rub=mo&cat=346",
    "https://www.bazos.cz/rss.php?rub=mo&cat=349",
]
# support multiple search URLs via comma-separated env var; fall back to the legacy single URL env var
_search_urls_value = os.getenv("BAZOS_SEARCH_URLS") or os.getenv("BAZOS_SEARCH_URL") or ",".join(DEFAULT_BAZOS_SEARCH_URLS)
BAZOS_SEARCH_URLS = [u.strip() for u in _search_urls_value.split(",") if u.strip()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
MIN_PRICE_EUR = float(os.getenv("MIN_PRICE_EUR", "0"))
MAX_PRICE_EUR = float(os.getenv("MAX_PRICE_EUR", "50"))  # strict max bound; entries without parseable price are excluded



def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception as ex:
            logger.warning("failed to parse seen file %s: %s", SEEN_FILE, ex)
            return set()
    return set()


def save_seen(s):
    # Sort and pretty-print for stable, human-readable state on disk.
    try:
        SEEN_FILE.write_text(json.dumps(sorted(s), indent=2, ensure_ascii=False) + "\n")
    except Exception as ex:
        logger.error("failed to persist seen file %s: %s", SEEN_FILE, ex)


def build_search_keywords(supported_models: list[str]) -> list:
    # use postmarketOS-supported models as search keywords
    pm_keywords = [t.lower() for t in supported_models]
    # dedupe
    seen = set()
    out = []
    for k in pm_keywords:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _bool_label(v) -> str:
    return "yes" if bool(v) else "no"


def _format_confidence(v) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "n/a"


def _is_czk_price(price: str | None) -> bool:
    if not price:
        return False
    return _contains_czk_currency(price)


def _format_price(price: str | None, price_eur) -> str | None:
    if price and _is_czk_price(price) and price_eur is not None:
        return f"{float(price_eur):.2f} EUR (from {price})"
    if price:
        return price
    if price_eur is not None:
        return f"{float(price_eur):.2f} EUR"
    return None


def format_message(item, eval_res):
    title = (item.get("title") or "").strip()
    # Some feed titles include trailing price (e.g. "model xyz: 30").
    # Keep the model line clean and show price only in the dedicated Price field.
    title = re.sub(r":\s*\d[\d\s.,]*\s*(?:€|eur|eur\.|czk|kč|kc)?\s*$", "", title, flags=re.IGNORECASE).strip()
    url = (item.get("url") or "").strip()
    price = _format_price(item.get("price"), item.get("price_eur"))

    parts = [
        "New listing match",
        f"Model: {title}",
    ]
    if price:
        parts.append(f"Price: {price}")
    parts.extend([
        f"postmarketOS support: {_bool_label(eval_res.get('postmarketos_support'))}",
        f"cluster suitability (k3s): {eval_res.get('k3s_suitability', 'unknown')}",
    ])

    reasons = [r for r in (eval_res.get("reasons") or []) if r]
    if reasons:
        parts.append(f"Why: {'; '.join(reasons[:2])}")
    if url:
        parts.append(f"URL: {url}")

    return "\n".join(parts)


def main_loop():
    seen = load_seen()
    while True:
        cycle_start = time.time()
        supported_models = get_supported_models()
        keywords = build_search_keywords(list(supported_models))
        if not keywords:
            logger.warning("No keywords configured; exiting.")
            return
        logger.info("Starting scan cycle: keywords=%d models=%d urls=%d seen=%d", len(keywords), len(supported_models), len(BAZOS_SEARCH_URLS), len(seen))
        all_matches = []
        scan_failures = 0
        for url in BAZOS_SEARCH_URLS:
            logger.debug("Searching URL: %s", url)
            try:
                matches = search_listings(url, keywords, supported_models=list(supported_models))
            except Exception as ex:
                logger.warning("search_listings failed for %s: %s", url, ex)
                matches = []
                scan_failures += 1
            for m in matches:
                # annotate match with source URL
                m['source_search_url'] = url
            all_matches.extend(matches)
        logger.info("Scan results: raw_matches=%d feed_failures=%d", len(all_matches), scan_failures)
        new_seen_count = 0
        filtered_out_count = 0
        for m in all_matches:
            # Defer listing page fetch until after keyword/model match to reduce source queries.
            enrich_listing_price(m)

            uid = m.get('url') or '' or (m.get('title') or '')[:200]

            # Apply strict price bounds: require parseable price and MIN_PRICE_EUR <= value <= MAX_PRICE_EUR
            price_eur = m.get('price_eur')
            if price_eur is None:
                logger.debug("filtering out match '%s': no parseable price (original raw price: %s)", uid, m.get('price'))
                # skip items without price when using strict bounds
                filtered_out_count += 1
                continue
            try:
                price_eur_value = float(price_eur)
                if price_eur_value < MIN_PRICE_EUR:
                    logger.debug("filtering out match '%s': price %.2f EUR is below MIN_PRICE_EUR (%s)", uid, price_eur_value, MIN_PRICE_EUR)
                    filtered_out_count += 1
                    continue
                if price_eur_value > MAX_PRICE_EUR:
                    logger.debug("filtering out match '%s': price %.2f EUR is above MAX_PRICE_EUR (%s)", uid, price_eur_value, MAX_PRICE_EUR)
                    filtered_out_count += 1
                    continue
            except Exception as ex:
                logger.debug("filtering out match '%s': error parsing price value '%s': %s", uid, price_eur, ex)
                filtered_out_count += 1
                continue

            if uid in seen:
                logger.debug("skipping seen uid=%s", uid)
                filtered_out_count += 1
                continue

            # additional confirmation: require strong match for keyword matches to avoid false positives
            matched_by = m.get('matched_by')
            match_type = m.get('match_type')
            logger.debug("processing match candidate: uid=%s match_type=%s matched_by=%s price=%s EUR", uid, match_type, matched_by, price_eur)
            if match_type == 'keyword' and matched_by and not strong_match(m.get('title') or '', matched_by):
                logger.debug("rejected fuzzy-only match for uid=%s matched_by=%s title=%s", uid, matched_by, (m.get('title') or '')[:120])
                filtered_out_count += 1
                continue

            # run evaluation
            eval_res = evaluate_listing({
                'title': m.get('title'),
                'url': m.get('url'),
                'price': m.get('price'),
                'price_eur': m.get('price_eur'),
                'published': m.get('published'),
            }, supported_models, min_price_eur=MIN_PRICE_EUR, max_price_eur=MAX_PRICE_EUR)
            logger.debug("evaluation result: postmarketos=%s confidence=%.2f k3s=%s ai=%s", eval_res.get('postmarketos_support'), eval_res.get('support_confidence'), eval_res.get('k3s_suitability'), eval_res.get('ai_used'))
            logger.debug("evaluation reasons=%s", eval_res.get('reasons'))

            # attach evaluation to message
            msg = format_message(m, eval_res)
            logger.info("New match: %s", uid)
            logger.debug("message=\n%s", msg)
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                ok = send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
                logger.debug("telegram send status=%s", ok)
                if ok:
                    seen.add(uid)
                    save_seen(seen)
                    new_seen_count += 1
                else:
                    logger.warning("not marking uid=%s as seen because telegram send failed", uid)
            else:
                # no telegram configured — still mark as seen to avoid reprocessing
                seen.add(uid)
                save_seen(seen)
                new_seen_count += 1
        save_seen(seen)
        logger.info(
            "Cycle done: new_seen=%d filtered=%d total_seen=%d duration_sec=%.2f",
            new_seen_count,
            filtered_out_count,
            len(seen),
            time.time() - cycle_start,
        )
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main_loop()
