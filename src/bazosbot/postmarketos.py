"""Utilities to fetch list of devices known to be supported by postmarketOS.

Read device names from a file pointed to by POSTMARKETOS_MODELS_FILE (one per line or JSON array).
This avoids downloading from the wiki.
"""
from typing import Set
from pathlib import Path
import json
import logging
import os

logger = logging.getLogger(__name__)
CACHE_FILE = Path("data/postmarketos_models.json")


def get_supported_models() -> Set[str]:
    """Return a set of device names using only the file from POSTMARKETOS_MODELS_FILE.

    The file may be a JSON array (e.g. ["Device A", "Device B"]) or a plain
    text file with one device name per line. If the env var is not set or the
    file is unreadable, fall back to the cached data/postmarketos_models.json if present.
    """
    env_path = os.getenv("POSTMARKETOS_MODELS_FILE")
    if env_path:
        try:
            env_file = Path(env_path)
            if env_file.exists():
                txt = env_file.read_text()
                try:
                    arr = json.loads(txt)
                    return {t.lower() for t in arr}
                except Exception:
                    lines = [line.strip() for line in txt.splitlines() if line.strip()]
                    return {line.lower() for line in lines}
        except Exception:
            logger.warning("failed to load POSTMARKETOS_MODELS_FILE=%s", env_path)

    # fallback to cached file if present
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text())
            return {t.lower() for t in data}
    except Exception:
        logger.warning("failed to read postmarketos cache file=%s", CACHE_FILE)

    # nothing available
    logger.debug("POSTMARKETOS_MODELS_FILE not provided or unreadable; returning empty set")
    return set()
