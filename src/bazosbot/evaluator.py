"""Evaluate listing compatibility with postmarketOS and k3s suitability.

This module intentionally uses heuristic-only evaluation based on fuzzy model
matching, device hardware specs (RAM/storage/curated score) and price checks.
"""

from typing import Dict, List, Optional, Tuple
import re
import difflib

# Minimum SequenceMatcher ratio (exclusive) for two tokens to be considered a
# fuzzy match. Must stay strictly-greater-than: e.g. "fairphone"/"iphone"
# score exactly 0.8, and an inclusive threshold would fuzzy-match them.
FUZZY_TOKEN_RATIO_THRESH = 0.8

# Tokens this short or shorter are too ambiguous to fuzzy-match reliably
# (e.g. "pi", "mi", "iii") and are only checked for an exact hit.
MIN_FUZZY_TOKEN_LEN = 4


def _hardware_k3s_label(model_meta: Optional[Dict]) -> Tuple[str, List[str]]:
    """Derive a k3s-suitability label from a matched device's RAM/storage/score metadata.

    Returns one of "yes" / "maybe" / "no" / "unknown" plus human-readable reasons.
    "unknown" means we have no hardware metadata to go on (e.g. a plain
    device-name-only models file) and callers should fall back to other signals.
    """
    reasons: List[str] = []
    if not model_meta:
        return "unknown", reasons

    score = model_meta.get("score")
    ram_raw = (model_meta.get("ram") or "").strip()
    storage_raw = (model_meta.get("storage") or "").strip()

    ram_values = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", ram_raw)]
    # Conservative: a listing could be the lowest-RAM variant of the model.
    ram_min_gb = min(ram_values) if ram_values else None

    storage_lower = storage_raw.lower()
    if "nvme" in storage_lower or "ssd" in storage_lower:
        storage_tier = "excellent"
    elif "ufs" in storage_lower:
        storage_tier = "good"
    elif "emmc" in storage_lower:
        storage_tier = "fair"
    else:
        storage_tier = "unknown"

    if ram_min_gb is None:
        ram_label = "unknown"
    elif ram_min_gb < 2:
        ram_label = "no"  # too little headroom for a k3s node alongside workloads
    elif ram_min_gb < 4:
        ram_label = "marginal"
    else:
        ram_label = "yes"

    reasons.append(
        f"RAM '{ram_raw or 'unknown'}' (min ~{ram_min_gb if ram_min_gb is not None else '?'} GB) -> {ram_label}"
    )
    reasons.append(f"Storage '{storage_raw or 'unknown'}' -> tier {storage_tier}")
    if score is not None:
        reasons.append(f"Curated device score {score}/5")

    if ram_label == "no":
        label = "no"
    elif ram_label == "unknown" and storage_tier == "unknown" and score is None:
        label = "unknown"
    elif ram_label == "yes" and storage_tier in ("excellent", "good"):
        label = "yes"
    else:
        label = "maybe"

    # Curated score acts as a sanity check/override on top of the raw spec heuristic.
    if isinstance(score, (int, float)):
        if score <= 1:
            label = "no"
            reasons.append("Curated score <=1 overrides suitability to 'no'")
        elif score <= 2 and label == "yes":
            label = "maybe"
            reasons.append("Curated score <=2 caps suitability at 'maybe'")

    return label, reasons


def _combine_k3s_labels(price_label: str, hardware_label: str) -> str:
    """Combine the price-bound label and the hardware-spec label into one verdict."""
    labels = {price_label, hardware_label}
    if "no" in labels:
        return "no"
    if labels == {"yes"}:
        return "yes"
    if labels <= {"unknown"}:
        return "unknown"
    return "maybe"


def _heuristic_evaluate(
    listing: Dict,
    supported_models: Dict[str, Dict],
    min_price_eur: float | None = None,
    max_price_eur: float | None = None,
) -> Dict:
    title = (listing.get("title") or "").lower()
    summary = (listing.get("summary") or "").lower()

    def _token_fuzzy_match(
        haystack: str,
        needle: str,
        token_ratio_thresh: float = FUZZY_TOKEN_RATIO_THRESH,
    ) -> bool:
        """Return True when each significant needle token is present approximately in haystack."""
        h_tokens = re.findall(r"\w+", haystack.lower())
        n_tokens = re.findall(r"\w+", needle.lower())
        if not h_tokens or not n_tokens:
            return False
        # Single-digit model numbers (e.g. the "4" in "Fairphone 4") are
        # significant and must not be dropped just for being short.
        important = [t for t in n_tokens if len(t) > 1 or t.isdigit()]
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
            # Short tokens are too ambiguous to fuzzy-match reliably, so
            # require an exact match for them but don't veto the whole
            # device on a miss — the digit-bearing tokens above already
            # carry the real discriminating power (e.g. "raspberry pi 4"
            # still requires "4" even if "pi" isn't found verbatim).
            if len(tok) < MIN_FUZZY_TOKEN_LEN:
                continue
            if not any(
                difflib.SequenceMatcher(None, tok, h).ratio() > token_ratio_thresh
                for h in h_tokens
            ):
                return False
        return True

    # match supported model by substring of page title against model names,
    # keeping each matched device's hardware metadata (score/ram/storage) alongside it
    support_reasons = []
    matched_models = []  # original-case display names, for messaging
    matched_meta: List[Dict] = []
    for name, meta in supported_models.items():
        if not name:
            continue
        if name in title or name in summary or _token_fuzzy_match(title, name) or _token_fuzzy_match(summary, name):
            matched_models.append((meta or {}).get("device") or name)
            matched_meta.append(meta or {})
    if matched_models:
        support_reasons.append(f"Matched postmarketOS models: {', '.join(matched_models[:5])}")
        postmarketos_support = True
        support_confidence = 0.95
    else:
        postmarketos_support = False
        support_confidence = 0.2
        support_reasons.append("No exact model match in title/summary")

    # price-bound heuristic aligned with configured runtime price bounds
    suitability_reasons = []
    price = listing.get("price_eur")
    if price is not None and min_price_eur is not None and max_price_eur is not None:
        if min_price_eur <= price <= max_price_eur:
            price_label = "yes"
            suitability_reasons.append(
                f"Price {price} EUR within range ({min_price_eur}-{max_price_eur})"
            )
        elif price < min_price_eur:
            price_label = "no"
            suitability_reasons.append(f"Price {price} EUR below minimum ({min_price_eur})")
        else:
            price_label = "no"
            suitability_reasons.append(f"Price {price} EUR exceeds maximum ({max_price_eur})")
    elif price is not None:
        # price known but no configured bounds; avoid contradicting main_loop filtering
        price_label = "unknown"
        suitability_reasons.append(f"Price {price} EUR but no configured price bounds available")
    else:
        price_label = "unknown"
        suitability_reasons.append("Price unknown; cannot assess price-based suitability")

    # hardware heuristic (RAM/storage/curated score) for the best-scoring matched device
    best_meta = max(matched_meta, key=lambda m: m.get("score") or 0) if matched_meta else None
    hardware_label, hardware_reasons = _hardware_k3s_label(best_meta)
    suitability_reasons.extend(hardware_reasons)

    k3s_suitability = _combine_k3s_labels(price_label, hardware_label)

    hardware = None
    if best_meta and any(best_meta.get(k) for k in ("ram", "storage", "score")):
        hardware = {
            "ram": best_meta.get("ram"),
            "storage": best_meta.get("storage"),
            "score": best_meta.get("score"),
            "tier": best_meta.get("tier"),
            "codename": best_meta.get("codename"),
        }

    return {
        "postmarketos_support": postmarketos_support,
        "support_confidence": support_confidence,
        "matched_models": matched_models,
        "hardware": hardware,
        "k3s_suitability": k3s_suitability,
        "support_reasons": support_reasons,
        "suitability_reasons": suitability_reasons,
        "reasons": support_reasons + suitability_reasons,
        "ai_used": False,
    }


def evaluate_listing(
    listing: Dict,
    supported_models: Dict[str, Dict],
    min_price_eur: float | None = None,
    max_price_eur: float | None = None,
) -> Dict:
    """Evaluate a listing using heuristic fuzzy matching only."""
    return _heuristic_evaluate(
        listing,
        supported_models,
        min_price_eur=min_price_eur,
        max_price_eur=max_price_eur,
    )
