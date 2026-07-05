"""Simple Telegram notifier using bot HTTP API via requests."""
import os
import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    url = TELEGRAM_API.format(token=token)
    payload = {"chat_id": chat_id, "text": text}
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False
