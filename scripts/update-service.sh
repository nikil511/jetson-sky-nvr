#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <mosquitto|frigate|sky-vision>"
    exit 1
fi

SERVICE="$1"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${HOME}/.config/jetson-sky-nvr/.env"

cd "${REPO_DIR}"
if [ "${SERVICE}" = "sky-vision" ]; then
    docker compose --env-file "${ENV_FILE}" build sky-vision
fi
docker compose --env-file "${ENV_FILE}" up -d --no-deps "${SERVICE}"
docker compose --env-file "${ENV_FILE}" ps "${SERVICE}"
