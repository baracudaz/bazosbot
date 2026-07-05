"""Utilities to fetch list of devices known to be supported by postmarketOS.

This uses the postmarketOS MediaWiki API to list pages in Category:Devices and
returns a normalized set of device names to match against bazos listings.
"""
from typing import Set
import requests
from pathlib import Path
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

WIKI_API = "https://wiki.postmarketos.org/w/api.php"
CACHE_FILE = Path("data/postmarketos_models.json")


def get_supported_models() -> Set[str]:
    """Return a set of device page titles from the postmarketOS wiki category 'Devices'.

    Priority:
    1. If environment variable POSTMARKETOS_MODELS_FILE points to a readable JSON/text file, load it.
    2. Try the wiki API and cache result.
    3. Fall back to HTML scraping of the category page if the API is blocked.
    4. Fall back to data/postmarketos_models.json cache.
    """
    # 1) env-provided file — check environment at call time (honor and return immediately if present)
    try:
        env_path = os.getenv('POSTMARKETOS_MODELS_FILE')
        if env_path:
            env_file = Path(env_path)
            if env_file.exists():
                txt = env_file.read_text()
                try:
                    arr = json.loads(txt)
                    return {t.lower() for t in arr}
                except Exception:
                    lines = [l.strip() for l in txt.splitlines() if l.strip()]
                    return {l.lower() for l in lines}
    except Exception:
        logger.debug("failed to load POSTMARKETOS_MODELS_FILE")

    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Devices",
        "cmlimit": "500",
        "format": "json",
    }

    headers = {"User-Agent": "bazosbot/1.0 (+https://github.com/)"}

    def _cache_and_return(titles: Set[str]) -> Set[str]:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(list(titles)))
        except Exception:
            logger.debug("failed to write postmarketos models cache")
        return titles

    # 2) Try API first
    try:
        r = requests.get(WIKI_API, params=params, timeout=15, headers=headers)
        if r.status_code == 403:
            logger.debug("wiki API returned 403; attempting HTML fallback")
            raise requests.HTTPError(f"403 Client Error: Forbidden for url: {r.url}")
        r.raise_for_status()
        data = r.json()
        members = data.get("query", {}).get("categorymembers", [])
        titles = {m.get("title", "").lower() for m in members if m.get("title")}
        return _cache_and_return(titles)
    except Exception as ex:
        logger.debug("failed to fetch postmarketOS models from wiki API: %s", ex)

    # 3) Try HTML scraping fallback (handles sites that block API requests)
    try:
        try:
            from bs4 import BeautifulSoup
        except Exception:
            logger.debug("beautifulsoup4 not available for HTML fallback")
            raise

        titles = set()
        # Try both the Category page and the Devices page which lists devices differently
        candidate_paths = ["/wiki/Category:Devices", "/wiki/Devices"]
        for base_path in candidate_paths:
            url = requests.compat.urljoin("https://wiki.postmarketos.org/", base_path)
            pages_visited = 0
            while url and pages_visited < 10:
                pages_visited += 1
                r = requests.get(url, timeout=15, headers=headers)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                # Prefer specific containers but fall back to content area
                containers = []
                mw_pages = soup.find(id="mw-pages")
                if mw_pages:
                    containers.append(mw_pages)
                mw_category = soup.find(class_="mw-category")
                if mw_category:
                    containers.append(mw_category)
                content = soup.find(id="mw-content-text")
                if content:
                    containers.append(content)

                for cont in containers:
                    for a in cont.find_all("a", href=True):
                        href = a["href"]
                        # only consider wiki article links (skip namespace links like 'Category:', 'Help:')
                        if href.startswith("/wiki/") and ":" not in href[len("/wiki/"):]:
                            text = a.get_text(strip=True)
                            if text:
                                titles.add(text.lower())
                # try to find a next page link (pagination) on category pages
                next_link = None
                if mw_pages:
                    for a in mw_pages.find_all("a", href=True):
                        if "from=" in a["href"]:
                            next_link = a["href"]
                            break
                # if no pagination, stop for this base_path
                if next_link:
                    url = requests.compat.urljoin("https://wiki.postmarketos.org/", next_link)
                else:
                    url = None
            if titles:
                return _cache_and_return(titles)
    except Exception as ex:
        logger.debug("HTML fallback for postmarketOS models failed: %s", ex)

    # 4) fallback to cache file
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text())
            return {t.lower() for t in data}
    except Exception:
        logger.debug("failed to read postmarketos cache")

    return set()
