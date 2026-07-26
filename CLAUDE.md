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

# Lint (required after any code change; resolve any remaining issues before finishing)
ruff check . --fix
```

Always work inside the project virtualenv (`.venv`).

There is no test suite in this repo currently.

Config lives in `.env` (copy from `.env.example`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`BAZOS_SEARCH_URLS`/`BAZOS_SEARCH_URL`, `CHECK_INTERVAL`, `LOG_LEVEL`, `POSTMARKETOS_MODELS_FILE`,
`MIN_PRICE_EUR`, `MAX_PRICE_EUR`, `MIN_K3S_SCORE`.

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
7. `main_loop` drops the match if the best-matched device's curated `score` is below
   `MIN_K3S_SCORE` (default `2`; `0` disables it) — this is the main lever for cutting notification
   noise from confirmed-but-weak-hardware devices without removing them from the models file.
   Matches with no scored device (e.g. a legacy plain-string models file) are never filtered by this.
8. `main.format_message()` builds the Telegram text; `notifier.send_telegram()` sends it.
9. Dedup state (`data/seen.json`) is keyed by listing URL (or a title fallback) and is only persisted
   once a Telegram send succeeds (or immediately if Telegram isn't configured) — so a failed send
   causes the listing to be retried next cycle.
10. `SIGINT`/`SIGTERM` trigger a graceful shutdown that persists `seen.json` before exit.

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
  `BAZOS_SEARCH_URLS` nor `BAZOS_SEARCH_URL` is set in `.env` (both `bazos.sk`/`bazos.cz` sites use
  numeric per-brand `cat=` IDs under `rub=mo`, independent per country — e.g. Xiaomi is `cat=451` on
  `.sk` but `cat=455` on `.cz`). Covers every mobile-brand category that has a device in
  `postmarketos_models.json` (Xiaomi, Google, Samsung, Huawei, Motorola, Nokia, Sony, plus the
  "other brands" catch-all for OnePlus/Fairphone/LG/etc.), so when adding devices from a brand not
  already covered, add that brand's `rub=mo&cat=` URL too or matches for it will never be scanned.
  Apple/Realme are intentionally omitted (no devices in the models file). Also includes the generic
  `rub=pc&cat=12` ("PC, Počítače") category, the most plausible bucket sellers use for SBCs like
  Raspberry Pi — bazos has no dedicated tablet or single-board-computer category.
- `postmarketos_models.json` — curated list of device entries (`device`, `tier`, `codename`, `score`,
  `ram`, `storage`) used as both the search keyword source and the compatibility list; drives the
  hardware side of `k3s_suitability` scoring (see step 6 above). `tier`/`codename` are
  documentation-only (traceable back to the upstream [pmaports](https://github.com/external-mirrors/pmaports)
  package, e.g. `device/community/device-fairphone-fp4`) and aren't read by any code — only
  `score`/`ram`/`storage` feed the evaluator. `score` is capped at 2 for `testing`-tier devices
  regardless of hardware, since postmarketOS support there is less mature/complete than `community`.
  Devices with an **archived** (`"Archived: Maintainer dropped package"`) or untraceable pmaports
  package are intentionally excluded — check upstream before re-adding one. Run
  `python -m src.scripts.check_postmarketos_models` to diff this file against live pmaports
  (`src/scripts/check_postmarketos_models.py`, a standalone read-only report deliberately kept out of
  `src/bazosbot/` since it isn't part of the bot's runtime import graph — no automated fixup).
- `seen.json` — persisted dedup set, sorted/pretty-printed on write.

## Notes

- Ruff config/cache exists (`.ruff_cache/`) — always run `ruff check . --fix` after making code
  changes and resolve any remaining issues before finishing.
- The bazos HTML fallback scraper is heuristic and may need tuning; RSS is the preferred/primary path.
