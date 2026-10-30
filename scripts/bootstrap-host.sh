#!/usr/bin/env bash
set -euo pipefail

echo "=== Jetson Sky NVR Host Bootstrap (L4T R35.6.5 / JetPack 5) ==="
cat /etc/nv_tegra_release

# 1. Ensure release-upgrades is locked to Prompt=never
sudo sed -i 's/^Prompt=.*/Prompt=never/' /etc/update-manager/release-upgrades

# 2. Ensure Docker daemon uses nvidia as default-runtime with log rotation
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
    "default-runtime": "nvidia",
    "runtimes": {
        "nvidia": {
            "path": "nvidia-container-runtime",
            "runtimeArgs": []
        }
    },
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "50m",
        "max-file": "3"
    }
}
JSON

sudo usermod -aG docker "${USER}" || true
sudo systemctl enable --now docker

# 3. Create capped NVR storage directories
sudo mkdir -p /srv/nvr/{frigate,sky-vision,mosquitto/data}
sudo chown -R "${USER}:${USER}" /srv/nvr

echo "=== Host Bootstrap Complete ==="
