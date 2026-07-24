#!/bin/bash
set -euo pipefail

FORCE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--force|-force) FORCE=true; shift ;;
    *) echo "Usage: $0 [-f|--force]" >&2; exit 1 ;;
  esac
done

cd "$(dirname "$(readlink -f "$0")")"

git fetch origin main

LOCAL_HASH=$(git rev-parse HEAD)
REMOTE_HASH=$(git rev-parse origin/main)
CONTAINER_RUNNING=$(docker ps -q -f name=^/bazosbot$ -f status=running)

if [[ "$FORCE" == "true" ]] || [[ "$LOCAL_HASH" != "$REMOTE_HASH" ]]; then
  echo "==> Rebuilding and deploying updates..."
  git reset --hard origin/main
  docker compose up --build -d
elif [[ -z "$CONTAINER_RUNNING" ]]; then
  echo "==> Container not running. Starting..."
  docker compose up -d
else
  echo "==> bazosbot is up to date and running."
fi
