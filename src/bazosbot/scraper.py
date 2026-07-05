"""Robust bazos listing extractor with RSS support.

If the provided category URL points to an RSS feed (contains 'rss' or ends with '.xml' or 'rss.php'),
parse the feed. Otherwise, fall back to heuristic HTML parsing but resolve relative links to absolute
and attempt to extract price and published date when available.
"""
from typing import List, Dict
import re
from urllib.parse import urljoin
import difflib

import requests
import feedparser
from bs4 import BeautifulSoup
import logging

logger = logging.getLogger(__name__)


def fuzzy_contains(text: str, key: str, ratio_thresh: float = 0.7, token_thresh: float = 0.6) -> bool:
    """Return True if `key` is approximately contained in `text`.

    Heuristics used (in order):
    - exact substring
    - SequenceMatcher ratio between key and whole text
    - token overlap (fraction of key tokens present in text)
    - short-window substring SequenceMatcher
    """
    if not key or not text:
        return False
    t = text.lower()
    k = key.lower()
    if k in t:
        return True
    # SequenceMatcher on whole strings (good for small differences/typos)
    ratio = difflib.SequenceMatcher(None, k, t).ratio()
    if ratio >= ratio_thresh:
        return True
    # token overlap: fraction of key tokens that appear in title tokens
    ktoks = set(re.findall(r"\w+", k))
    ttoks = set(re.findall(r"\w+", t))
    if ktoks and ttoks:
        overlap = len(ktoks & ttoks) / len(ktoks)
        if overlap >= token_thresh:
            return True
    # short-window approximate substring matching
    # slide a window of length around the key length across the title
    win = max(10, len(k) + 10)
    for i in range(0, max(1, len(t) - win + 1)):
        sub = t[i : i + win]
        if difflib.SequenceMatcher(None, k, sub).ratio() >= ratio_thresh:
            return True
    return False


PRICE_RE = re.compile(r"(\d[\d\s,.]*\s*(?:€|eur|eur\.|sk|kč|kc))", re.IGNORECASE)

# normalize price formats like "199 €", "1 999 Kč" into integer EUR when possible
CURRENCY_MAP = {
    '€': 'EUR', 'eur': 'EUR', 'eur.': 'EUR',
    'sk': 'SK', 'kč': 'CZK', 'kc': 'CZK'
}

def parse_price_to_eur(price_str: str) -> float | None:
    if not price_str:
        return None
    s = price_str.strip().lower()
    # extract number
    num = re.sub(r"[^0-9,.]", "", s)
    # replace commas with dots if looks like decimal
    num = num.replace(',', '.')
    try:
        val = float(num)
    except Exception:
        # try removing spaces
        try:
            val = float(num.replace(' ', ''))
        except Exception:
            logger.debug("parse_price_to_eur failed to parse numeric from %s", price_str)
            return None
    # determine currency by suffix
    if 'k' in s and ('kč' in s or 'kc' in s):
        # assume CZK -> convert approx 25 CZK per EUR
        eur = round(val / 25.0, 2)
        logger.debug("parsed price %s as %s EUR (assumed CZK)", price_str, eur)
        return eur
    if 'sk' in s:
        # old Slovak crowns — unknown; return None
        logger.debug("parse_price_to_eur encountered SK currency for %s", price_str)
        return None
    # default assume EUR
    eur = round(val, 2)
    logger.debug("parsed price %s as %s EUR", price_str, eur)
    return eur



def fetch_rss_entries(url: str) -> List[Dict]:
    """Fetch and parse RSS/Atom entries from a feed URL."""
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        entries = []
        logger.debug("RSS feed parsed entries=%d", len(parsed.entries))
        for e in parsed.entries:
            title = (e.get("title") or "").strip()
            link = e.get("link") or e.get("id") or ""
            published = e.get("published") or e.get("updated") or None
            summary = (e.get("summary") or "").strip()
            price = extract_price(title + " " + summary)
            price_eur = parse_price_to_eur(price) if price else None

            # If RSS didn't include price, try fetching the listing page to extract price
            if price_eur is None and link:
                try:
                    logger.debug("RSS entry missing price, fetching page to extract: %s", link)
                    page = requests.get(link, timeout=10)
                    if page.status_code == 200 and page.text:
                        p = extract_price(page.text)
                        if p:
                            pe = parse_price_to_eur(p)
                            if pe is not None:
                                price = p
                                price_eur = pe
                                logger.debug("extracted price from page=%s price_eur=%s", p, pe)
                except Exception as ex:
                    logger.debug("failed to fetch entry page for price extraction %s: %s", link, ex)

            logger.debug("entry title=%s link=%s price=%s price_eur=%s", title[:80], link, price, price_eur)
            entries.append({"title": title.lower(), "url": link, "published": published, "price": price, "price_eur": price_eur})
        return entries
    except Exception as ex:
        logger.exception("failed to fetch or parse RSS %s: %s", url, ex)
        return []


def extract_price(text: str):
    if not text:
        return None
    m = PRICE_RE.search(text)
    if m:
        found = m.group(1).strip()
        logger.debug("extract_price found=%s in text=%s", found, (text or '')[:120])
        return found
    return None


def fetch_category_html(url: str) -> str:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.text


def extract_listings_from_html(html: str, base_url: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    # Try common bazos patterns first
    # Many bazos pages use links inside <a class="inzerat"> or article/listing blocks
    anchors = []
    anchors.extend(soup.find_all('a', class_=lambda v: v and 'inzerat' in v))
    anchors.extend(soup.find_all('a', href=True))

    seen = set()
    for a in anchors:
        href = a.get('href')
        if not href:
            continue
        full = urljoin(base_url, href)
        title = (a.get_text(separator=' ', strip=True) or '').lower()
        if not title:
            # maybe link contains img with alt
            img = a.find('img')
            if img and img.get('alt'):
                title = img.get('alt').strip().lower()
        if not title:
            continue
        if full in seen:
            continue
        seen.add(full)
        # Attempt to find price nearby
        price = None
        # look for sibling nodes that may contain price
        parent = a.parent
        if parent:
            text_blob = parent.get_text(separator=' ', strip=True)
            price = extract_price(text_blob)
        price_eur = parse_price_to_eur(price) if price else None
        listings.append({"title": title, "url": full, "price": price, "price_eur": price_eur})
    return listings


def search_listings(category_url: str, keywords: List[str]) -> List[Dict]:
    """Return listings from either RSS or HTML that match any keyword.

    Matching is done case-insensitively against normalized titles. Returned items contain:
    title (lowercase), url (absolute), optional published, optional price.
    """
    keys = [k.lower() for k in keywords if k]
    results = []
    # detect RSS-like URL
    if 'rss' in category_url or category_url.endswith('.xml') or 'rss.php' in category_url:
        entries = fetch_rss_entries(category_url)
        for e in entries:
            title = e.get('title', '') or ''
            if keys:
                matched = False
                for k in keys:
                    if fuzzy_contains(title, k):
                        logger.debug("fuzzy matched entry title=%s keyword=%s", title[:80], k)
                        results.append(e)
                        matched = True
                        break
                if matched:
                    continue
            else:
                results.append(e)
        return results

    # fallback to HTML
    html = fetch_category_html(category_url)
    items = extract_listings_from_html(html, category_url)
    for it in items:
        title = it.get('title', '') or ''
        if keys:
            for k in keys:
                if fuzzy_contains(title, k):
                    logger.debug("fuzzy matched html entry title=%s keyword=%s", title[:80], k)
                    results.append(it)
                    break
        else:
            results.append(it)
    return results
