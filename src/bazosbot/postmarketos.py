"""Utilities to fetch list of devices known to be supported by postmarketOS.

This uses the postmarketOS MediaWiki API to list pages in Category:Devices and
returns a normalized set of device names to match against bazos listings.
"""
from typing import Set
import requests

WIKI_API = "https://wiki.postmarketos.org/w/api.php"


def get_supported_models() -> Set[str]:
    """Return a set of device page titles from the postmarketOS wiki category 'Devices'.

    Falls back to an empty set on errors — callers should handle empty as "no filter".
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
        return titles
    except Exception:
        return set()
