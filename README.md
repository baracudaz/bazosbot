# bazosbot — lowballer monitor (prototype)

This is an initial Python prototype that monitors bazos.sk and bazos.cz for listings matching
postmarketOS-supported devices and notifies via Telegram (alerts-only mode).

## Quick start

1. Create a venv and install dependencies:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

3. Run:
   ```bash
   python -m src.bazosbot.main
   ```

## Configuration

- **Search URLs**: By default, the bot scans the RSS feeds listed in `data/bazos_search_urls.json`. You can customize this file or override it by setting `BAZOS_SEARCH_URLS` (comma-separated list) or `BAZOS_SEARCH_URL` in `.env`.
- **Supported Devices**: The list of target device model names is loaded from the file specified by the `POSTMARKETOS_MODELS_FILE` env var, which defaults to `data/postmarketos_models.json`.
- **Price Filtering**: Set `MIN_PRICE_EUR` and `MAX_PRICE_EUR` in `.env` to restrict matches. Listings with missing or unparseable prices are automatically skipped.

## Graceful Shutdown

The bot registers handlers for `SIGINT` (Ctrl+C) and `SIGTERM` (system termination). When these signals are received, the bot executes a graceful shutdown and ensures the list of processed listing IDs is persisted to `data/seen.json` to avoid duplicate notifications on restart.

## Docker & Deployment

To build and run directly via Docker:

```bash
docker build -t bazosbot:latest .
docker run --env-file .env -v "$(pwd)/data:/app/data" --rm bazosbot:latest
```

Or run via Docker Compose:

```bash
docker compose up --build -d
docker compose logs -f
```

### Automated Deployment

The repository includes a `deploy.sh` script. Executing it will:

1. Fetch changes from `origin/main`.
2. Rebuild and restart the container if new commits are detected.
3. Automatically start the container via `docker compose` if it is not currently running.

## Notes

- Czech prices are converted from CZK to EUR for filtering and notifications.
- The bazos scraper is heuristic-based and may need tuning for accurate parsing.
