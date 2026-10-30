# Jetson Sky NVR Operations Guide 

## Architecture
- **Host OS:** NVIDIA L4T R35.6.5 (`35.6.5-20260701022834`, JetPack 5.1.6, Kernel `5.10.216-tegra`, CUDA `11.4`, TensorRT `8.5.2.2`)
- **Services (`~/jetson-sky-nvr/docker-compose.yml`):**
  - `mosquitto`: Local MQTT broker (`127.0.0.1:1883` and internal Docker bridge)
  - `frigate`: Frigate NVR (`frigate:0.18.2-tensorrt-jp5`, fallback `ghcr.io/gonuit/frigate-jetpack5:0.17.2-tensorrt-jp5`) with `preset-jetson-h265` NVDEC decode and ONNX Runtime GPU (`device: Tensorrt` FP16 engine)
  - `sky-vision`: Standalone Sky CV Laboratory service (`sky-vision:1.0.0`) reading from `rtsp://frigate:8554/athens_sky`
- **Credentials:** Stored outside Git at `~/.config/jetson-sky-nvr/.env` (`0600`).

## Updating or Restarting a Single Service Without Host Interruption
```bash
~/jetson-sky-nvr/scripts/update-service.sh sky-vision
~/jetson-sky-nvr/scripts/update-service.sh frigate
~/jetson-sky-nvr/scripts/update-service.sh mosquitto
```

## Healthcheck & Benchmarks
```bash
~/jetson-sky-nvr/scripts/healthcheck.sh
```

## Live classification views
- Dashboard: `http://<JETSON_IP>:8080` — latest overlay, cloud percentage/oktas,
  condition, apparent image motion and a rolling 24-hour cloud-cover chart.
- Frigate: `http://<JETSON_IP>:5000` — `athens_sky` is the original camera;
  `athens_sky_vision` is the annotated classification tile. The overlay is updated
  approximately every 5 minutes, carried as a repeated 1-fps H.264 stream by
  go2rtc's built-in FFmpeg integration. No object detection or recording is enabled
  on the derived tile, and H.265 hardware decode remains enabled on the original.
- Dashboard API: `/api/latest`, `/api/history` (last 24 hours),
  `/latest_sky_debug.jpg`, `/stream.mjpeg` (internal Frigate source).
- HTTP runs in a separate thread in sky-vision, with no additional dependencies.
  Port 8080 is intended for the trusted LAN, just like the existing Frigate port.
  Latest files are replaced atomically. The dashboard flags observations older
  than 120 seconds as stale. Cloud estimates are experimental and apply only to
  the masked camera region; motion direction is image-relative, not a wind bearing.
- Rebuild after source/dashboard edits: `scripts/update-service.sh sky-vision`.
  Restart Frigate after camera configuration edits: `docker compose --env-file
  "$HOME/.config/jetson-sky-nvr/.env" restart frigate`.
- Frigate formats go2rtc stream strings for environment substitution before
  launching go2rtc. Avoid unescaped FFmpeg `{input}` placeholders inside stream
  strings; the custom MJPEG input above uses its explicit internal HTTP URL.
- Low-fps integration: the derived stream must use a keyframe every frame (`-g 1`)
  so new viewers and Frigate can start decoding immediately. Use the explicit
  `#video=-c:v libx264 ...` codec arguments: go2rtc's default `#video=h264`
  template overrides earlier `#raw=-g 1` with its own `-g 50`, causing long waits
  and Frigate watchdog restarts. Keep the derived camera's short probe settings.
