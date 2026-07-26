"""Utilities to fetch list of devices known to be supported by postmarketOS.

Read device names (and optional hardware metadata) from a file pointed to by
POSTMARKETOS_MODELS_FILE. This avoids downloading from the wiki.
"""

from typing import Dict
from pathlib import Path
import json
import logging
import os

logger = logging.getLogger(__name__)


def get_supported_models(models_file: str | Path | None = None) -> Dict[str, Dict]:
    """Return a mapping of lowercased device name -> metadata dict.

    The file may be:
    - a JSON array of device-name strings, e.g. ["Device A", "Device B"]
    - a JSON array of objects with a "device" key plus optional hardware metadata
      (e.g. "score", "ram", "storage"), e.g. [{"device": "Device A", "score": 5, ...}]
    - a plain text file with one device name per line

    String entries and plain-text lines have no metadata and map to an empty dict.
    """
    path_str = models_file or os.getenv("POSTMARKETOS_MODELS_FILE")
    if path_str:
        try:
            file_path = Path(path_str)
            if file_path.exists():
                txt = file_path.read_text()
                try:
                    arr = json.loads(txt)
                except Exception:
                    lines = [line.strip() for line in txt.splitlines() if line.strip()]
                    return {line.lower(): {} for line in lines}

                models: Dict[str, Dict] = {}
                for entry in arr:
                    if isinstance(entry, str):
                        if entry:
                            models[entry.lower()] = {}
                    elif isinstance(entry, dict):
                        name = entry.get("device")
                        if name:
                            models[name.lower()] = entry
                return models
            else:
                logger.warning("POSTMARKETOS_MODELS_FILE does not exist: %s", path_str)
        except Exception:
            logger.warning("failed to load POSTMARKETOS_MODELS_FILE=%s", path_str)

    # nothing available
    logger.debug(
        "POSTMARKETOS_MODELS_FILE not provided or unreadable; returning empty mapping"
    )
    return {}
