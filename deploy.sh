#!/bin/bash
set -euo pipefail

FORCE=false

# Parse command line flags (supports -f, --force, -force)
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--force|-force)
      FORCE=true
      shift
      ;;
    *)
      echo "Usage: $0 [-f | --force]" >&2
      exit 1
      ;;
  esac
done

cd "$(dirname "$(readlink -f "$0")")"

git fetch origin

if [ "$FORCE" = true ]; then
  echo "==> Force flag set. Pulling and rebuilding..."
  git reset --hard origin/main
  docker compose up --build -d
elif [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "==> New changes detected. Pulling and rebuilding the image..."
  git reset --hard origin/main
  docker compose up --build -d
elif [ -z "$(docker ps -q -f name=^/bazosbot$ -f status=running)" ]; then
  echo "==> bazosbot is not running. Starting it..."
  docker compose up -d
else
  echo "==> bazosbot is up to date and running."
fi
