"""Utilities to fetch list of devices known to be supported by postmarketOS.

This uses the postmarketOS MediaWiki API to list pages in Category:Devices and
returns a normalized set of device names to match against bazos listings.
"""
from typing import Set
import requests
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)

WIKI_API = "https://wiki.postmarketos.org/w/api.php"
CACHE_FILE = Path("data/postmarketos_models.json")


def get_supported_models() -> Set[str]:
    """Return a set of device page titles from the postmarketOS wiki category 'Devices'.

    On success, cache the result to data/postmarketos_models.json. On failure, fall back to the
    cached file if present. Callers should handle empty set as "no filter".
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Devices",
        "cmlimit": "500",
        "format": "json",
    }
    try:
        r = requests.get(WIKI_API, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        members = data.get("query", {}).get("categorymembers", [])
        titles = {m.get("title", "").lower() for m in members if m.get("title")}
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(list(titles)))
        except Exception:
            logger.debug("failed to write postmarketos models cache")
        return titles
    except Exception as ex:
        logger.debug("failed to fetch postmarketOS models from wiki: %s", ex)
        # fallback to cache
        try:
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text())
                return {t.lower() for t in data}
        except Exception:
            logger.debug("failed to read postmarketos cache")
        return set()
