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


def strong_match(text: str, key: str, ratio_token_thresh: float = 0.8) -> bool:
    """Stricter confirmation: every important token in key must appear approximately in text.

    Important tokens are tokens containing digits or length > 2. Uses SequenceMatcher per token
    to allow minor typos.
    """
    if not key or not text:
        return False
    t = text.lower()
    k = key.lower()
    # quick exact containment
    if k in t:
        return True
    ktoks = [tok for tok in re.findall(r"\w+", k) if (any(c.isdigit() for c in tok) or len(tok) > 2)]
    if not ktoks:
        return False
    for tok in ktoks:
        # if token appears exactly, good
        if tok in t:
            continue
        # allow fuzzy token match against any substring window
        found = False
        # try matching against title tokens
        ttoks = re.findall(r"\w+", t)
        for wt in ttoks:
            if difflib.SequenceMatcher(None, tok, wt).ratio() >= ratio_token_thresh:
                found = True
                break
        if not found:
            return False
    return True


PRICE_RE = re.compile(r"(\d[\d\s,.]*\s*(?:€|eur|eur\.|sk|kč|kc))", re.IGNORECASE)

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
    """Fetch and parse RSS/Atom entries from a feed URL.

    Keep this lightweight: do not fetch individual listing pages here.
    """
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


def enrich_listing_price(listing: Dict) -> Dict:
    """Fill missing price for a single listing by fetching its details page."""
    if listing.get("price_eur") is not None:
        return listing
    link = listing.get("url")
    if not link:
        return listing
    try:
        logger.debug("Listing missing price, fetching page to extract: %s", link)
        page = requests.get(link, timeout=10)
        if page.status_code == 200 and page.text:
            p = extract_price(page.text)
            if p:
                pe = parse_price_to_eur(p)
                if pe is not None:
                    listing["price"] = p
                    listing["price_eur"] = pe
                    logger.debug("extracted price from page=%s price_eur=%s", p, pe)
    except Exception as ex:
        logger.debug("failed to fetch listing page for price extraction %s: %s", link, ex)
    return listing


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


def search_listings(category_url: str, keywords: List[str], supported_models: List[str] | None = None) -> List[Dict]:
    """Return listings from either RSS or HTML that match any keyword or supported model.

    Matching is done case-insensitively and with fuzzy matching for typos. `supported_models`
    is an optional list of postmarketOS model names to consider when matching; when provided
    the function will match listings against both keywords and supported model names.
    Returned items contain: title (lowercase), url (absolute), optional published, optional price.
    """
    keys = [k.lower() for k in (keywords or []) if k]
    models = [m.lower() for m in (supported_models or []) if m]
    results = []
    # detect RSS-like URL
    if 'rss' in category_url or category_url.endswith('.xml') or 'rss.php' in category_url:
        entries = fetch_rss_entries(category_url)
        for e in entries:
            title = e.get('title', '') or ''
            url = e.get('link', '') or e.get('url', '') or ''
            searchable = f"{title} {url}".strip()
            matched = False
            # check keywords
            if keys:
                for k in keys:
                    if fuzzy_contains(searchable, k):
                        logger.debug("fuzzy matched entry title/url=%s keyword=%s", searchable[:120], k)
                        e['matched_by'] = k
                        e['match_type'] = 'keyword'
                        results.append(e)
                        matched = True
                        break
            if matched:
                continue
            # check supported models
            if models:
                for m in models:
                    if fuzzy_contains(searchable, m):
                        logger.debug("fuzzy matched entry title/url=%s model=%s", searchable[:120], m[:60])
                        e['matched_by'] = m
                        e['match_type'] = 'model'
                        results.append(e)
                        matched = True
                        break
            if matched:
                continue
            if not keys and not models:
                results.append(e)
        return results

    # fallback to HTML
    html = fetch_category_html(category_url)
    items = extract_listings_from_html(html, category_url)
    for it in items:
        title = it.get('title', '') or ''
        url = it.get('url', '') or ''
        searchable = f"{title} {url}".strip()
        matched = False
        if keys:
            for k in keys:
                if fuzzy_contains(searchable, k):
                    logger.debug("fuzzy matched html entry title/url=%s keyword=%s", searchable[:120], k)
                    it['matched_by'] = k
                    it['match_type'] = 'keyword'
                    results.append(it)
                    matched = True
                    break
        if matched:
            continue
        if models:
            for m in models:
                if fuzzy_contains(searchable, m):
                    logger.debug("fuzzy matched html entry title/url=%s model=%s", searchable[:120], m[:60])
                    it['matched_by'] = m
                    it['match_type'] = 'model'
                    results.append(it)
                    matched = True
                    break
        if matched:
            continue
        if not keys and not models:
            results.append(it)
    return results
