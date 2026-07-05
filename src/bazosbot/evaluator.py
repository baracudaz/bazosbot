"""Evaluate listing compatibility with postmarketOS and k3s suitability.

This module provides a heuristic evaluator and optional OpenAI-assisted evaluator.
Environment variables:
- OPENAI_API_KEY: if present, will attempt to call OpenAI Chat Completions API
- OPENAI_API_URL: optional override for API base (default https://api.openai.com)

The primary exported function is `evaluate_listing(listing, supported_models)` which
returns a dict with keys:
- postmarketos_support: bool
- support_confidence: float (0-1)
- k3s_suitability: 'yes'|'no'|'unknown'
- reasons: list[str]
- ai_used: bool
- ai_raw: optional raw response when AI used

If AI is unavailable or call fails, falls back to heuristics.
"""
from typing import Dict, Set
import os
import re
import json
import requests
import logging

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com")


def _heuristic_evaluate(listing: Dict, supported_models: Set[str]) -> Dict:
    title = (listing.get("title") or "").lower()
    summary = (listing.get("summary") or "").lower()
    reasons = []

    # match supported model by substring of page title against model names
    matched_models = []
    for m in supported_models:
        if m and m.lower() in title:
            matched_models.append(m)
        elif m and m.lower() in summary:
            matched_models.append(m)
    if matched_models:
        reasons.append(f"Matched postmarketOS models: {', '.join(matched_models[:5])}")
        postmarketos_support = True
        support_confidence = 0.95
    else:
        postmarketos_support = False
        support_confidence = 0.2
        reasons.append("No exact model match in title/summary")

    # basic k3s suitability heuristic: price and presence of words like 'battery'
    price = listing.get("price_eur")
    if price is not None:
        if price <= float(os.getenv("PRICE_CAP_EUR", "50")):
            k3s_suitability = "yes"
            reasons.append(f"Price {price} EUR within cap")
        else:
            k3s_suitability = "no"
            reasons.append(f"Price {price} EUR exceeds cap")
    else:
        k3s_suitability = "unknown"
        reasons.append("Price unknown")

    return {
        "postmarketos_support": postmarketos_support,
        "support_confidence": support_confidence,
        "k3s_suitability": k3s_suitability,
        "reasons": reasons,
        "ai_used": False,
    }


def _call_openai(prompt: str) -> str | None:
    if not OPENAI_API_KEY:
        return None
    url = OPENAI_API_URL.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": "You are an assistant that evaluates mobile phone listings for postmarketOS compatibility and k3s suitability. Reply with a JSON object only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 300,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        # extract assistant message
        return data.get("choices", [])[0].get("message", {}).get("content")
    except Exception as ex:
        logger.exception("OpenAI call failed: %s", ex)
        return None


def _extract_json(text: str) -> Dict | None:
    if not text:
        return None
    # find first {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def evaluate_listing(listing: Dict, supported_models: Set[str]) -> Dict:
    """Evaluate a listing using AI if possible, otherwise heuristics.

    listing: dict with keys title, url, price, price_eur, published, summary(optional)
    supported_models: set of postmarketOS-supported model page titles
    """
    # First run heuristic quickly
    h = _heuristic_evaluate(listing, supported_models)

    # If AI key present, attempt to ask AI for a short JSON evaluation
    if OPENAI_API_KEY:
        prompt_lines = [
            "Listing:\n",
            f"Title: {listing.get('title')}",
            f"URL: {listing.get('url')}",
            f"Price: {listing.get('price')} ({listing.get('price_eur')})",
            f"Summary: {listing.get('summary') or ''}\n",
            "Known postmarketOS models (sample up to 20):",
        ]
        sample = list(supported_models)[:20]
        prompt_lines.append(", ".join(sample))
        prompt_lines.append(
            "\nReturn a JSON object with keys: postmarketos_support (yes/no), support_confidence(0-1), k3s_suitability (yes/no/unknown), reasons (array of short strings)."
        )
        prompt = "\n".join(prompt_lines)
        ai_raw = _call_openai(prompt)
        parsed = _extract_json(ai_raw)
        if parsed:
            try:
                post = parsed.get("postmarketos_support")
                post_bool = True if str(post).lower() in ("yes", "true", "1") else False
                return {
                    "postmarketos_support": post_bool,
                    "support_confidence": float(parsed.get("support_confidence", 0.5)),
                    "k3s_suitability": parsed.get("k3s_suitability", "unknown"),
                    "reasons": parsed.get("reasons", []) if isinstance(parsed.get("reasons", []), list) else [str(parsed.get("reasons"))],
                    "ai_used": True,
                    "ai_raw": ai_raw,
                }
            except Exception:
                logger.debug("failed to parse AI JSON, falling back to heuristic")
                # fall through to heuristic
    # return heuristic result
    return h
