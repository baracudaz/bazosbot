# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python prototype that polls bazos.sk / bazos.cz classifieds (via RSS feeds) for listings matching
postmarketOS-supported devices within a price range, and sends Telegram notifications for new matches
(alerts-only, no auto-buying).

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run
python -m src.bazosbot.main

# Lint (required after any code change — see .github/copilot-instructions.md)
ruff check . --fix
```

There is no test suite in this repo currently.

Config lives in `.env` (copy from `.env.example`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`BAZOS_SEARCH_URLS`/`BAZOS_SEARCH_URL`, `CHECK_INTERVAL`, `LOG_LEVEL`, `POSTMARKETOS_MODELS_FILE`,
`MIN_PRICE_EUR`, `MAX_PRICE_EUR`.

### Deployment

`deploy.sh` fetches `origin/main`, and if the local repo is behind (or `-f`/`--force` is passed), does
`git reset --hard origin/main` + `docker compose up --build -d`. It also starts the container if it's
not running. Meant to be run on the deploy host, not locally — it hard-resets the working tree.

```bash
docker compose up --build -d
docker compose logs -f
```

## Architecture

Single long-running loop (`main.main_loop`) with no web server or database — state is two JSON files
under `data/`.

**Pipeline per cycle** (`src/bazosbot/main.py`):
1. Load postmarketOS device list via `postmarketos.get_supported_models()` — reads
   `POSTMARKETOS_MODELS_FILE` (default `data/postmarketos_models.json`) and returns a
   `{lowercased device name: metadata dict}` mapping (metadata is `{}` for plain string/text-line
   entries, or `{device, score, ram, storage}` for the current JSON-object format). Model names
   become the search keywords directly; there is no separate keyword list.
2. For each URL in `BAZOS_SEARCH_URLS`, `scraper.search_listings()` fetches and filters listings —
   RSS parsing (`feedparser`) if the URL looks like a feed, otherwise heuristic HTML scraping via
   BeautifulSoup as a fallback.
3. Matching in `scraper.py` is two-tier: `fuzzy_contains()` (substring / SequenceMatcher / token
   overlap) does the initial filter inside `search_listings`, then `main_loop` re-checks
   keyword-type matches with the stricter `strong_match()` (every significant token must match) to
   cut false positives. Model-type matches are not re-checked.
4. `scraper.enrich_listing_price()` fetches the individual listing page to backfill price only if the
   feed entry didn't have one.
5. Price filtering is strict: listings without a parseable price, or outside
   `[MIN_PRICE_EUR, MAX_PRICE_EUR]`, are dropped. CZK prices are auto-converted to EUR at a fixed
   ~25:1 rate (`scraper.parse_price_to_eur`); old Slovak koruna ("sk") is treated as unparseable.
6. `evaluator.evaluate_listing()` re-derives postmarketOS support (fuzzy/token match against the same
   model map) and combines two independent heuristics into `k3s_suitability`
   (`"yes"`/`"maybe"`/`"no"`/`"unknown"`, via `_combine_k3s_labels`): a price-bounds check, and
   `_hardware_k3s_label()` which scores the best-matched device's RAM (parses the lowest advertised
   variant — conservative, since a listing may be the cheapest/lowest-RAM SKU), storage tier
   (nvme/ssd > ufs > emmc), and the curated `score` field (`score<=1` forces `"no"`, `score<=2` caps
   at `"maybe"`). Purely heuristic — no AI/network calls despite the `ai_used` field in its output
   (always `False`).
7. `main.format_message()` builds the Telegram text; `notifier.send_telegram()` sends it.
8. Dedup state (`data/seen.json`) is keyed by listing URL (or a title fallback) and is only persisted
   once a Telegram send succeeds (or immediately if Telegram isn't configured) — so a failed send
   causes the listing to be retried next cycle.
9. `SIGINT`/`SIGTERM` trigger a graceful shutdown that persists `seen.json` before exit.

**Module boundaries:**
- `postmarketos.py` — loads the supported-device list from a file (JSON array or newline-delimited
  text). No network access to the postmarketOS wiki.
- `scraper.py` — all bazos-specific fetching, parsing, price extraction/currency conversion, and
  fuzzy-matching logic.
- `evaluator.py` — heuristic-only scoring of an already-matched listing; independent of the scraper's
  own matching pass (some duplication is intentional — evaluator re-verifies rather than trusting the
  scraper's tag).
- `notifier.py` — thin Telegram Bot API wrapper.
- `main.py` — orchestration, env/config loading, message formatting, seen-state persistence, signal
  handling.

**Data files (`data/`):**
- `bazos_search_urls.json` — default list of bazos RSS category URLs, used when neither
  `BAZOS_SEARCH_URLS` nor `BAZOS_SEARCH_URL` is set in `.env`.
- `postmarketos_models.json` — curated list of device entries (`device`, `score`, `ram`, `storage`)
  used as both the search keyword source and the compatibility list; drives the hardware side of
  `k3s_suitability` scoring (see step 6 above).
- `seen.json` — persisted dedup set, sorted/pretty-printed on write.

## Notes

- Ruff config/cache exists (`.ruff_cache/`) — run `ruff check . --fix` after edits and resolve any
  remaining issues before finishing, per `.github/copilot-instructions.md`.
- The bazos HTML fallback scraper is heuristic and may need tuning; RSS is the preferred/primary path.
