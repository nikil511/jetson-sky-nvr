#!/usr/bin/env bash
set -euo pipefail

echo "=== 1. L4T & Kernel Version ==="
cat /etc/nv_tegra_release
uname -r

echo "=== 2. HDMI Display Resolution ==="
DISPLAY=:0 xrandr | grep -E "Screen 0|HDMI-0|\*" || true

echo "=== 3. Storage Usage ==="
df -h / /srv/nvr

echo "=== 4. Docker Containers ==="
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"

echo "=== 5. Frigate Version & Detector Stats ==="
curl -fsSL http://127.0.0.1:5000/api/version && echo ""
curl -fsSL http://127.0.0.1:5000/api/stats | jq '{detectors, cameras: (.cameras | map_values({camera_fps, detection_fps, process_fps})), service}'

echo "=== 6. Sky-Vision Latest Observation ==="
if [ -f /srv/nvr/sky-vision/latest_observation.json ]; then
    cat /srv/nvr/sky-vision/latest_observation.json
else
    echo "Waiting for first sky-vision observation..."
fi
