"""Evaluate listing compatibility with postmarketOS and k3s suitability.

This module intentionally uses heuristic-only evaluation based on fuzzy model
matching and price checks.
"""
from typing import Dict, Set
import os
import re
import difflib
from dotenv import load_dotenv

load_dotenv()


def _heuristic_evaluate(listing: Dict, supported_models: Set[str]) -> Dict:
    title = (listing.get("title") or "").lower()
    summary = (listing.get("summary") or "").lower()
    reasons = []

    def _token_fuzzy_match(haystack: str, needle: str, token_ratio_thresh: float = 0.8) -> bool:
        """Return True when each significant needle token is present approximately in haystack."""
        h_tokens = re.findall(r"\w+", haystack.lower())
        n_tokens = re.findall(r"\w+", needle.lower())
        if not h_tokens or not n_tokens:
            return False
        important = [t for t in n_tokens if len(t) > 1]
        if not important:
            return False
        for tok in important:
            # Numeric-bearing tokens should match exactly (e.g. 9t, 4x).
            if any(ch.isdigit() for ch in tok):
                if tok not in h_tokens:
                    return False
                continue
            if tok in h_tokens:
                continue
            if not any(difflib.SequenceMatcher(None, tok, h).ratio() >= token_ratio_thresh for h in h_tokens):
                return False
        return True

    # match supported model by substring of page title against model names
    matched_models = []
    for m in supported_models:
        if m and m.lower() in title:
            matched_models.append(m)
        elif m and m.lower() in summary:
            matched_models.append(m)
        elif m and (_token_fuzzy_match(title, m) or _token_fuzzy_match(summary, m)):
            matched_models.append(m)
    if matched_models:
        reasons.append(f"Matched postmarketOS models: {', '.join(matched_models[:5])}")
        postmarketos_support = True
        support_confidence = 0.95
    else:
        postmarketos_support = False
        support_confidence = 0.2
        reasons.append("No exact model match in title/summary")

    # basic k3s suitability heuristic aligned with runtime price bounds
    price = listing.get("price_eur")
    if price is not None:
        min_price = float(os.getenv("MIN_PRICE_EUR", "0"))
        max_price = float(os.getenv("MAX_PRICE_EUR", "50"))
        if min_price <= price <= max_price:
            k3s_suitability = "yes"
            reasons.append(f"Price {price} EUR within range ({min_price}-{max_price})")
        elif price < min_price:
            k3s_suitability = "no"
            reasons.append(f"Price {price} EUR below minimum ({min_price})")
        else:
            k3s_suitability = "no"
            reasons.append(f"Price {price} EUR exceeds maximum ({max_price})")
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

def evaluate_listing(listing: Dict, supported_models: Set[str]) -> Dict:
    """Evaluate a listing using heuristic fuzzy matching only."""
    return _heuristic_evaluate(listing, supported_models)
