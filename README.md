bazosbot — lowballer monitor (prototype)

This is an initial Python prototype that monitors bazos.sk for listings matching
postmarketOS-supported devices and notifies via Telegram (alerts-only mode).

Quick start

1. Create a venv and install dependencies:
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt

2. Copy .env.example to .env and set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

3. Run:
   python -m src.bazosbot.main

Notes
- postmarketOS device list is fetched from the postmarketOS wiki category "Devices".
- The bazos scraper is heuristic-based and may need tuning for accurate parsing.
- Useful next steps: implement robust bazos parsing, rate-limiting, retries, and CLI.
