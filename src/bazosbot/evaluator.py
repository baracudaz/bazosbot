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
import difflib
import requests
import logging
from pathlib import Path
import time
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com")
# Local AI server (LM Studio or similar). If set, will be tried when AI_PROVIDER=="local" or if OPENAI_API_KEY is absent.
LOCAL_AI_URL = os.getenv("LOCAL_AI_URL")
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").lower()  # auto|openai|local|none
LOCAL_AI_MODEL = os.getenv("LOCAL_AI_MODEL")
AI_MODEL = os.getenv("AI_MODEL", "gpt-3.5-turbo")
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "500"))
AI_TIMEOUT_SEC = int(os.getenv("AI_TIMEOUT_SEC", "30"))
AI_FORCE_JSON = os.getenv("AI_FORCE_JSON", "true").lower() in ("1", "true", "yes")

# cache evaluated listings to avoid repeated AI calls
EVAL_CACHE_FILE = Path("data/eval_cache.json")
CACHE_TTL = int(os.getenv("EVAL_CACHE_TTL", "86400"))  # seconds, default 1 day
_CACHE_MEMO: Dict | None = None


def _load_cache() -> Dict:
    global _CACHE_MEMO
    if _CACHE_MEMO is not None:
        return _CACHE_MEMO
    try:
        if EVAL_CACHE_FILE.exists():
            _CACHE_MEMO = json.loads(EVAL_CACHE_FILE.read_text())
            return _CACHE_MEMO
    except Exception:
        _CACHE_MEMO = {}
        return {}
    _CACHE_MEMO = {}
    return {}


def _save_cache(cache: Dict):
    global _CACHE_MEMO
    try:
        EVAL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        EVAL_CACHE_FILE.write_text(json.dumps(cache))
        _CACHE_MEMO = cache
    except Exception:
        logger.debug("failed to write eval cache")


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


def _extract_chat_choice_text(choice: Dict) -> str | None:
    if not isinstance(choice, dict):
        return None
    msg = choice.get("message", {})
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    text = choice.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def _base_chat_payload(prompt: str, model: str) -> Dict:
    payload: Dict = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You evaluate listings and respond with JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": AI_MAX_TOKENS,
    }
    if AI_FORCE_JSON:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _try_repair_json(raw_text: str) -> Dict | None:
    if not raw_text:
        return None
    repair_prompt = (
        "Convert the following text into exactly one JSON object with keys "
        "postmarketos_support, support_confidence, k3s_suitability, reasons. "
        "Return JSON only.\n\n"
        f"Text:\n{raw_text}"
    )
    repaired = _call_ai(repair_prompt)
    return _extract_json(repaired)


def _call_local(prompt: str) -> str | None:
    """Call a local LM Studio-like HTTP generation endpoint.

    The endpoint is configurable via LOCAL_AI_URL. This function is defensive and
    tries several common response shapes, returning raw text when found.
    """
    if not LOCAL_AI_URL:
        return None

    def _try_openai_compatible(base_url: str) -> str | None:
        models_url = f"{base_url}/models"
        chat_url = f"{base_url}/chat/completions"
        model = LOCAL_AI_MODEL
        if not model:
            try:
                mr = requests.get(models_url, timeout=10)
                mr.raise_for_status()
                mdata = mr.json()
                items = mdata.get("data", []) if isinstance(mdata, dict) else []
                if items and isinstance(items[0], dict):
                    model = items[0].get("id")
            except Exception as ex:
                logger.debug("failed to fetch local model list: %s", ex)
        if not model:
            model = "local-model"

        payload = _base_chat_payload(prompt, model)
        try:
            r = requests.post(chat_url, json=payload, timeout=AI_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices", []) if isinstance(data, dict) else []
            if choices and isinstance(choices[0], dict):
                text = _extract_chat_choice_text(choices[0])
                if text is not None:
                    return text
            logger.debug("local OpenAI-compatible response missing usable content")
            return r.text
        except requests.HTTPError as ex:
            # Some local servers reject response_format; retry once without it.
            status = ex.response.status_code if ex.response is not None else None
            if AI_FORCE_JSON and status in (400, 404, 422):
                fallback_payload = _base_chat_payload(prompt, model)
                fallback_payload.pop("response_format", None)
                try:
                    r = requests.post(chat_url, json=fallback_payload, timeout=AI_TIMEOUT_SEC)
                    r.raise_for_status()
                    data = r.json()
                    choices = data.get("choices", []) if isinstance(data, dict) else []
                    if choices and isinstance(choices[0], dict):
                        text = _extract_chat_choice_text(choices[0])
                        if text is not None:
                            return text
                    return r.text
                except Exception as fallback_ex:
                    logger.debug("local OpenAI-compatible fallback failed: %s", fallback_ex)
                    return None
            logger.debug("local OpenAI-compatible HTTP error: %s", ex)
            return None
        except Exception as ex:
            logger.debug("local OpenAI-compatible call failed: %s", ex)
            return None

    local = LOCAL_AI_URL.rstrip("/")
    if local.endswith("/v1"):
        resp = _try_openai_compatible(local)
        if resp is not None:
            return resp
    elif "/v1/" in local:
        prefix = local.split("/v1/")[0] + "/v1"
        resp = _try_openai_compatible(prefix)
        if resp is not None:
            return resp
    else:
        resp = _try_openai_compatible(f"{local}/v1")
        if resp is not None:
            return resp

    payload = {"prompt": prompt, "max_new_tokens": 300, "temperature": 0.0}
    try:
        r = requests.post(LOCAL_AI_URL, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        # common keys: 'text', 'response', 'results'[0]['output_text']
        if isinstance(data, dict):
            if "text" in data and isinstance(data["text"], str):
                return data["text"]
            if "response" in data and isinstance(data["response"], str):
                return data["response"]
            if "results" in data and isinstance(data["results"], list) and data["results"]:
                first = data["results"][0]
                # try common nested keys
                for k in ("output", "output_text", "text"):
                    if k in first and isinstance(first[k], str):
                        return first[k]
        # fallback to raw text
        return r.text
    except Exception as ex:
        logger.exception("Local AI call failed: %s", ex)
        return None


def _call_openai(prompt: str) -> str | None:
    if not OPENAI_API_KEY:
        return None
    url = OPENAI_API_URL.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": "You are an assistant that evaluates mobile phone listings for postmarketOS compatibility and k3s suitability. Reply with a JSON object only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": AI_MAX_TOKENS,
    }
    if AI_FORCE_JSON:
        payload["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=AI_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices", []) if isinstance(data, dict) else []
        if choices and isinstance(choices[0], dict):
            text = _extract_chat_choice_text(choices[0])
            if text is not None:
                return text
        return r.text
    except requests.HTTPError as ex:
        # Some providers ignore/deny response_format. Retry once without it.
        status = ex.response.status_code if ex.response is not None else None
        if AI_FORCE_JSON and status in (400, 404, 422):
            payload.pop("response_format", None)
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=AI_TIMEOUT_SEC)
                r.raise_for_status()
                data = r.json()
                choices = data.get("choices", []) if isinstance(data, dict) else []
                if choices and isinstance(choices[0], dict):
                    text = _extract_chat_choice_text(choices[0])
                    if text is not None:
                        return text
                return r.text
            except Exception as fallback_ex:
                logger.exception("OpenAI fallback call failed: %s", fallback_ex)
                return None
        logger.exception("OpenAI HTTP call failed: %s", ex)
        return None
    except Exception as ex:
        logger.exception("OpenAI call failed: %s", ex)
        return None


def _call_ai(prompt: str) -> str | None:
    """Provider-agnostic AI call. Tries provider according to AI_PROVIDER or availability."""
    # explicit provider
    if AI_PROVIDER == "none":
        return None
    if AI_PROVIDER == "local":
        return _call_local(prompt)
    if AI_PROVIDER == "openai":
        return _call_openai(prompt)
    # auto: prefer local if LOCAL_AI_URL set, otherwise OpenAI
    if LOCAL_AI_URL:
        resp = _call_local(prompt)
        if resp is not None:
            return resp
    if OPENAI_API_KEY:
        resp = _call_openai(prompt)
        if resp is not None:
            return resp
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

    # If AI provider configured, attempt to ask AI for a short JSON evaluation
    # Check cache first
    cache = _load_cache()
    url = listing.get('url')
    if url and url in cache:
        entry = cache[url]
        ts = entry.get('ts', 0)
        if time.time() - ts < CACHE_TTL:
            logger.debug("using cached evaluation for %s", url)
            return entry.get('result')

    if AI_PROVIDER != "none":
        prompt_lines = [
            "You are an assistant that evaluates mobile phone listings for postmarketOS compatibility and whether the device is suitable for running k3s in a low-cost cluster.\n",
            "Return ONLY a single JSON object (no additional text) with the following schema:\n",
            "{\n  \"postmarketos_support\": \"yes\" or \"no\",\n  \"support_confidence\": float between 0.0 and 1.0,\n  \"k3s_suitability\": \"yes\" or \"no\" or \"unknown\",\n  \"reasons\": [\"short reason strings\"]\n}\n",
            "Evaluate the following listing. Be conservative: if unsure about support, answer postmarketos_support: \"no\" and support_confidence <= 0.5. Use k3s_suitability=\"yes\" only when price and basic suitability match.\n",
            f"Listing Title: {listing.get('title')}",
            f"Listing URL: {listing.get('url')}",
            f"Price (raw): {listing.get('price')}",
            f"Price (EUR): {listing.get('price_eur')}",
            f"Summary: {listing.get('summary') or ''}",
            "Known postmarketOS-supported model names (sample):",
        ]
        sample = list(supported_models)[:40]
        prompt_lines.append(", ".join(sample))
        prompt = "\n".join(prompt_lines)
        ai_raw = _call_ai(prompt)
        parsed = _extract_json(ai_raw)
        if not parsed and ai_raw:
            parsed = _try_repair_json(ai_raw)
        if parsed:
            try:
                post = parsed.get("postmarketos_support")
                post_bool = True if str(post).lower() in ("yes", "true", "1") else False
                result = {
                    "postmarketos_support": post_bool,
                    "support_confidence": float(parsed.get("support_confidence", 0.5)),
                    "k3s_suitability": parsed.get("k3s_suitability", "unknown"),
                    "reasons": parsed.get("reasons", []) if isinstance(parsed.get("reasons", []), list) else [str(parsed.get("reasons"))],
                    "ai_used": True,
                    "ai_raw": ai_raw,
                }
                # cache result
                if url:
                    cache[url] = {"ts": int(time.time()), "result": result}
                    _save_cache(cache)
                return result
            except Exception:
                logger.debug("failed to parse AI JSON, falling back to heuristic")

    # return heuristic result
    return h
