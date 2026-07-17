#!/bin/bash
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

git fetch origin

if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  git reset --hard origin/main
  docker compose up --build -d
fi