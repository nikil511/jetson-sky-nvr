# Recovery & Rollback Procedures

## 1. Frigate Rollback (`0.18.2-tensorrt-jp5` -> Known-Good `0.17.2-tensorrt-jp5`)
```bash
~/jetson-sky-nvr/scripts/rollback-frigate.sh ghcr.io/gonuit/frigate-jetpack5:0.17.2-tensorrt-jp5
```

## 2. HDMI 1080p60 Display Recovery (Denon AVR -> 4K TV)
- Backups are stored at `/root/pre-r35.6.5-backup/`.
- Live switch over SSH:
  ```bash
  DISPLAY=:0 xrandr --output HDMI-0 --mode 1920x1080 --rate 60
  ```

## 3. Storage Cap Protection
- NVR media is stored under `/srv/nvr/frigate` (2-day motion retention) and `/srv/nvr/sky-vision` (1500 MB automatic ring-buffer pruning).
