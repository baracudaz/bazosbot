"""Simple bazos.sk scraper — minimal, heuristic-based.

This module exposes `search_listings` which fetches the configured bazos category
page and finds links whose title/text mentions any of the target keywords.
"""
from typing import List, Dict
import requests
from bs4 import BeautifulSoup


def fetch_category(url: str) -> str:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.text


def extract_listings(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    # heuristic: find all anchor tags with hrefs; many bazos pages use <a class="inzerat"/>
    for a in soup.find_all('a', href=True):
        text = (a.get_text(separator=' ', strip=True) or '').lower()
        href = a['href']
        listings.append({'title': text, 'url': href})
    return listings


def search_listings(category_url: str, keywords: List[str]) -> List[Dict]:
    html = fetch_category(category_url)
    items = extract_listings(html)
    matches = []
    keys = [k.lower() for k in keywords if k]
    for it in items:
        for k in keys:
            if k in it['title']:
                matches.append(it)
                break
    return matches
