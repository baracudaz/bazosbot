"""Simple Telegram notifier using bot HTTP API via requests."""

import requests
import logging

logger = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        logger.warning("send_telegram called without token/chat_id")
        return False
    url = TELEGRAM_API.format(token=token)
    payload = {"chat_id": chat_id, "text": text}
    try:
        logger.debug("sending telegram to chat_id=%s payload=%s", chat_id, text[:200])
        r = requests.post(url, data=payload, timeout=10)
        ok = r.status_code == 200
        if not ok:
            logger.warning(
                "telegram send failed status=%s text=%s", r.status_code, r.text[:300]
            )
        return ok
    except Exception as ex:
        logger.error("telegram send exception: %s", ex)
        return False
