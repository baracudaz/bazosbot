#!/bin/bash
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

git fetch origin

if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  git reset --hard origin/main
  docker compose up --build -d
elif [ -z "$(docker ps -q -f name=^/bazosbot$ -f status=running)" ]; then
  echo "bazosbot is not running. Starting it..."
  docker compose up -d
fi