"""Diff data/postmarketos_models.json against the live postmarketOS pmaports repo.

Detects the failure mode that motivated this script: a device silently going
"archived" (or vanishing entirely) upstream while our curated file still lists
it as good. Reports three things:

- STALE: devices in our file whose package is no longer community/testing upstream
  (archived, downstream-only, or gone entirely)
- TIER CHANGED: devices whose community/testing tier differs from what we recorded
- NEW UPSTREAM: community/testing device packages upstream that we don't have at all

pmaports has no RAM/storage/score data, so NEW UPSTREAM entries are reported for
awareness only -- adding them still requires a manual/LLM pass to fill in specs,
same as when this file was first built.

A device's postmarketOS support doesn't always live in its own device-<codename>
package. Some devices share a generic chipset package (e.g. device-qcom-sm7150,
device-qcom-msm8953) instead, in one of two patterns seen in pmaports:
  1. a per-device firmware-<vendor>-<codename> directory alongside the shared
     device- package (e.g. firmware-xiaomi-davinci for Xiaomi Mi 9T)
  2. bundled into ONE umbrella firmware package via _<vendor>_<codename>_commit=
     APKBUILD variables (e.g. firmware-qcom-msm8953 covers xiaomi-markw,
     xiaomi-mido, motorola-potter, ... all in a single APKBUILD)
A device whose own device-<codename> package is archived/missing but who shows
up via either pattern is still supported -- this script treats that as the
"real" signal and won't flag it STALE. (This isn't foolproof: it can't detect a
device that's *only* referenced from within some other file we don't scan.)

Usage:
    python -m src.scripts.check_postmarketos_models
"""
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

REPO = "external-mirrors/pmaports"
TREE_API = f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPO}/main/device"
TIERS_FOR_NEW_DEVICES = ("community", "testing")
TIERS_TO_SCAN = ("community", "testing", "archived", "downstream")
TIER_RANK = {"community": 0, "testing": 1, "downstream": 2, "archived": 3}
FETCH_WORKERS = 16
COMMIT_VAR_RE = re.compile(r"_([a-z0-9]+)_([a-z0-9]+)_commit=")

MODELS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "postmarketos_models.json"


def _github_auth_headers() -> dict[str, str]:
    """Best-effort: use the `gh` CLI's cached token (if logged in) so this hits
    GitHub's authenticated rate limit (5000 req/hour) instead of the
    unauthenticated one (60 req/hour, shared per IP -- easy to exhaust just from
    running this script a few times). Silently falls back to unauthenticated if
    `gh` isn't installed or isn't logged in.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return {}
    token = result.stdout.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _fetch_repo_tree() -> list[str]:
    """Return every directory path in the repo, in a single API call.

    Listing each device/<tier> directory individually (the GitHub "contents"
    API) needs several paginated requests per tier and quickly trips the
    unauthenticated rate limit (60 requests/hour) once combined with the
    device-* and firmware-* passes. The "git trees" API returns the whole
    repo listing in one request instead.
    """
    resp = requests.get(TREE_API, timeout=60, headers=_github_auth_headers())
    resp.raise_for_status()
    data = resp.json()
    if data.get("truncated"):
        print("  warning: GitHub truncated the repo tree response — some entries may be missing",
              file=sys.stderr)
    return [entry["path"] for entry in data["tree"] if entry["type"] == "tree"]


def _dirs_by_tier(tree_paths: list[str], prefix: str) -> dict[str, list[str]]:
    """Group top-level device/<tier>/<prefix>* directory names by tier (prefix stripped)."""
    result: dict[str, list[str]] = {tier: [] for tier in TIERS_TO_SCAN}
    for path in tree_paths:
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "device" and parts[1] in result and parts[2].startswith(prefix):
            result[parts[1]].append(parts[2][len(prefix):])
    return result


def _fetch_raw(tier: str, dirname: str) -> str | None:
    url = f"{RAW_ROOT}/{tier}/{dirname}/APKBUILD"
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.text


def _fetch_pkgdesc(tier: str, codename: str) -> str | None:
    text = _fetch_raw(tier, f"device-{codename}")
    if not text:
        return None
    m = re.search(r'^pkgdesc="([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None


def _print_progress(done: int, total: int, width: int = 30) -> None:
    filled = width if total == 0 else int(width * done / total)
    bar = "#" * filled + "." * (width - filled)
    print(f"\r  [{bar}] {done}/{total}", end="", file=sys.stderr, flush=True)


def fetch_upstream(device_dirs: dict[str, list[str]]) -> dict[str, dict]:
    """Return {codename: {"tier": ..., "pkgdesc": ...}} across all scanned tiers.

    When a codename appears in multiple tiers (e.g. an old downstream attempt
    plus an active testing port), the "best" tier wins: community > testing >
    downstream > archived.
    """
    upstream: dict[str, dict] = {}
    for tier in TIERS_TO_SCAN:
        for codename in device_dirs[tier]:
            if codename in upstream and TIER_RANK[tier] >= TIER_RANK[upstream[codename]["tier"]]:
                continue
            upstream[codename] = {"tier": tier, "pkgdesc": None}
    total = len(upstream)
    print(f"  fetching {total} package descriptions ({FETCH_WORKERS} in parallel) ...", file=sys.stderr)
    done = 0
    _print_progress(done, total)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_pkgdesc, info["tier"], codename): codename
            for codename, info in upstream.items()
        }
        for future in as_completed(futures):
            codename = futures[future]
            upstream[codename]["pkgdesc"] = future.result()
            done += 1
            _print_progress(done, total)
    print(file=sys.stderr)
    return upstream


def fetch_shared_chipset_support(fw_dirs: dict[str, list[str]]) -> dict[str, str]:
    """Return {device_codename: tier} for devices supported via a shared chipset
    package rather than their own device-<codename> package (see module docstring
    for the two patterns detected). device_codename is "<vendor>-<model>", matching
    our own codename convention (e.g. "xiaomi-markw").
    """
    tier_rank_local = TIER_RANK
    result: dict[str, str] = {}

    def _register(codename: str, tier: str) -> None:
        if codename in result and tier_rank_local[tier] >= tier_rank_local[result[codename]]:
            return
        result[codename] = tier

    # pattern 1: firmware-<vendor>-<codename> directory name itself
    for tier, names in fw_dirs.items():
        for name in names:
            if "-" in name:
                _register(name, tier)

    # pattern 2: bundled _<vendor>_<codename>_commit= variables inside umbrella packages
    all_fw = [(tier, name) for tier, names in fw_dirs.items() for name in names]
    total = len(all_fw)
    print(f"  scanning {total} firmware package sources for bundled device firmware "
          f"({FETCH_WORKERS} in parallel) ...", file=sys.stderr)
    done = 0
    _print_progress(done, total)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_raw, tier, f"firmware-{name}"): tier for tier, name in all_fw}
        for future in as_completed(futures):
            tier = futures[future]
            text = future.result()
            done += 1
            _print_progress(done, total)
            if not text:
                continue
            for m in COMMIT_VAR_RE.finditer(text):
                _register(f"{m.group(1)}-{m.group(2)}", tier)
    print(file=sys.stderr)
    return result


def load_ours() -> list[dict]:
    return json.loads(MODELS_FILE.read_text())


def main() -> None:
    print(f"Checking {MODELS_FILE.relative_to(MODELS_FILE.parent.parent)} against "
          f"https://github.com/{REPO} (main)\n", file=sys.stderr)

    ours = load_ours()

    print("  fetching repo tree ...", file=sys.stderr)
    tree_paths = _fetch_repo_tree()
    device_dirs = _dirs_by_tier(tree_paths, "device-")
    fw_dirs = _dirs_by_tier(tree_paths, "firmware-")

    upstream = fetch_upstream(device_dirs)
    shared_support = fetch_shared_chipset_support(fw_dirs)

    ours_by_codename = {d["codename"]: d for d in ours if d.get("codename")}
    no_codename = [d for d in ours if not d.get("codename")]

    stale = []
    tier_changed = []
    for codename, entry in ours_by_codename.items():
        up = upstream.get(codename)
        up_tier = up["tier"] if up else None
        shared_tier = shared_support.get(codename)

        # Prefer whichever signal (own device package vs. shared chipset package)
        # shows active support, picking the better tier if both do.
        effective_tier = None
        for candidate in (up_tier, shared_tier):
            if candidate in ("community", "testing") and (
                effective_tier is None or TIER_RANK[candidate] < TIER_RANK[effective_tier]
            ):
                effective_tier = candidate

        if effective_tier is None:
            stale.append((entry, up_tier))
        elif effective_tier != entry.get("tier"):
            tier_changed.append((entry, effective_tier))

    new_upstream = [
        (codename, info)
        for codename, info in upstream.items()
        if info["tier"] in TIERS_FOR_NEW_DEVICES and codename not in ours_by_codename
    ]
    new_upstream.sort(key=lambda x: (x[1]["tier"], x[0]))

    print("=" * 70)
    print("postmarketOS device list refresh check")
    print("=" * 70)
    print(f"Our file:  {len(ours)} devices ({len(no_codename)} without a codename, skipped)")
    print(f"Upstream:  {len(upstream)} device packages across {', '.join(TIERS_TO_SCAN)}")
    print()

    print(f"STALE — no longer community/testing upstream ({len(stale)}):")
    if not stale:
        print("  (none)")
    for entry, found_tier in sorted(stale, key=lambda x: x[0]["device"]):
        where = f"now {found_tier}" if found_tier else "not found in any tier — may have been fully removed"
        print(f"  - {entry['device']}  [codename: {entry['codename']}]  "
              f"our tier: {entry.get('tier', '?')}  →  {where}")
    print()

    print(f"TIER CHANGED ({len(tier_changed)}):")
    if not tier_changed:
        print("  (none)")
    for entry, new_tier in sorted(tier_changed, key=lambda x: x[0]["device"]):
        arrow = "upgrade" if new_tier == "community" else "downgrade"
        print(f"  - {entry['device']}  [codename: {entry['codename']}]  "
              f"our tier: {entry.get('tier', '?')} → upstream: {new_tier}  ({arrow})")
    print()

    print(f"NEW UPSTREAM DEVICES not in our file ({len(new_upstream)}):")
    print("  (RAM/storage/score aren't in pmaports — these need a manual/LLM pass before adding)")
    print("  (most of this list is expected noise — only consider actual phones (older/used/cheap")
    print("   models people would resell), NOT Chromebooks, laptops, tablets, e-readers, gaming")
    print("   handhelds, smartwatches, routers/NAS, Apple devices, or generic/qemu/shared-chipset")
    print("   placeholder packages)")
    if not new_upstream:
        print("  (none)")
    for codename, info in new_upstream:
        print(f"  - [{info['tier']}] {info['pkgdesc'] or '(no pkgdesc)'}  [codename: {codename}]")
    print()

    if no_codename:
        print(f"NOTE: {len(no_codename)} entries in our file have no \"codename\" field and "
              f"were skipped (can't be checked against upstream):")
        for entry in no_codename:
            print(f"  - {entry.get('device')}")


if __name__ == "__main__":
    main()
