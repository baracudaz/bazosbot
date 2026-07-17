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
def get_supported_models(models_file: str | Path | None = None) -> Set[str]:
    """Return a set of device names using the provided models file or the POSTMARKETOS_MODELS_FILE env var.

    The file may be a JSON array (e.g. ["Device A", "Device B"]) or a plain
    text file with one device name per line.
    """
    path_str = models_file or os.getenv("POSTMARKETOS_MODELS_FILE")
    if path_str:
        try:
            file_path = Path(path_str)
            if file_path.exists():
                txt = file_path.read_text()
                try:
                    arr = json.loads(txt)
                    return {t.lower() for t in arr}
                except Exception:
                    lines = [line.strip() for line in txt.splitlines() if line.strip()]
                    return {line.lower() for line in lines}
            else:
                logger.warning("POSTMARKETOS_MODELS_FILE does not exist: %s", path_str)
        except Exception:
            logger.warning("failed to load POSTMARKETOS_MODELS_FILE=%s", path_str)

    # nothing available
    logger.debug(
        "POSTMARKETOS_MODELS_FILE not provided or unreadable; returning empty set"
    )
    return set()
