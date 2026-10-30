#!/usr/bin/env bash
set -euo pipefail

TARGET_IMAGE="${1:-ghcr.io/gonuit/frigate-jetpack5:0.17.2-tensorrt-jp5}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${HOME}/.config/jetson-sky-nvr/.env"

echo "Rolling back Frigate to ${TARGET_IMAGE}..."
if grep -q "^FRIGATE_IMAGE=" "${ENV_FILE}"; then
    sed -i "s|^FRIGATE_IMAGE=.*|FRIGATE_IMAGE=${TARGET_IMAGE}|" "${ENV_FILE}"
else
    echo "FRIGATE_IMAGE=${TARGET_IMAGE}" >> "${ENV_FILE}"
fi

cd "${REPO_DIR}"
docker compose --env-file "${ENV_FILE}" up -d --no-deps frigate
docker compose --env-file "${ENV_FILE}" ps frigate
