"""LAN dashboard, historical frame playback API, and low-rate MJPEG bridge for Frigate/go2rtc."""
import datetime as dt
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SAFE_MEDIA_RE = re.compile(r"^/media/(debug|raw)/(\d{4}-\d{2}-\d{2})/((?:sky|raw)_\d{8}_\d{6}\.jpg)$")


def start_server(data_dir: Path, port: int = 8080):
    cache = {"at": 0.0, "body": b"[]", "frames_at": 0.0, "frames_body": b"[]"}
    cache_lock = threading.Lock()

    def history():
        with cache_lock:
            if time.monotonic() - cache["at"] < 10:
                return cache["body"]
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
            rows = []
            for day in sorted({cutoff.date(), dt.datetime.now(dt.timezone.utc).date()}):
                for path in sorted((data_dir / "observations" / day.isoformat()).glob("obs_*.json")):
                    try:
                        row = json.loads(path.read_text())
                        if dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) >= cutoff:
                            rows.append({k: row[k] for k in ("timestamp", "cloud_pct", "oktas")})
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
            cache.update(at=time.monotonic(), body=json.dumps(rows[-3000:]).encode())
            return cache["body"]

    def frames_list():
        with cache_lock:
            if time.monotonic() - cache["frames_at"] < 5:
                return cache["frames_body"]
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
            items = []
            for day in sorted({cutoff.date(), dt.datetime.now(dt.timezone.utc).date()}):
                day_s = day.isoformat()
                obs_dir = data_dir / "observations" / day_s
                for obs_path in sorted(obs_dir.glob("obs_*.json")):
                    try:
                        stamp = obs_path.stem.replace("obs_", "")
                        dbg_rel = f"/media/debug/{day_s}/sky_{stamp}.jpg"
                        raw_rel = f"/media/raw/{day_s}/raw_{stamp}.jpg"
                        if not (data_dir / "debug" / day_s / f"sky_{stamp}.jpg").exists():
                            continue
                        row = json.loads(obs_path.read_text())
                        if dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) < cutoff:
                            continue
                        has_raw = (data_dir / "raw" / day_s / f"raw_{stamp}.jpg").exists()
                        items.append(
                            {
                                "stamp": stamp,
                                "timestamp": row.get("timestamp"),
                                "cloud_pct": row.get("cloud_pct"),
                                "oktas": row.get("oktas"),
                                "sky_condition": row.get("sky_condition"),
                                "flow_speed_px_s": row.get("flow_speed_px_s"),
                                "flow_azimuth_deg": row.get("flow_azimuth_deg"),
                                "cloud_speed_m_s": row.get("cloud_speed_m_s"),
                                "estimated_ceiling_m": row.get("estimated_ceiling_m") or row.get("lcl_ceiling_m"),
                                "is_daytime": row.get("is_daytime"),
                                "debug_url": dbg_rel,
                                "raw_url": raw_rel if has_raw else dbg_rel,
                                "has_raw": has_raw,
                            }
                        )
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
            cache.update(frames_at=time.monotonic(), frames_body=json.dumps(items[-288:]).encode())
            return cache["frames_body"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_body(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = urlsplit(self.path).path
            try:
                if route == "/stream.mjpeg":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        try:
                            frame = (data_dir / "latest_sky_debug.jpg").read_bytes()
                        except FileNotFoundError:
                            time.sleep(1)
                            continue
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(frame)).encode()
                            + b"\r\n\r\n"
                            + frame
                            + b"\r\n"
                        )
                        self.wfile.flush()
                        time.sleep(1)
                elif route == "/api/history":
                    self.send_body(history(), "application/json")
                elif route == "/api/frames":
                    self.send_body(frames_list(), "application/json")
                elif route in ("/api/latest", "/latest_observation.json"):
                    self.send_body((data_dir / "latest_observation.json").read_bytes(), "application/json")
                elif route == "/latest_sky_debug.jpg":
                    self.send_body((data_dir / "latest_sky_debug.jpg").read_bytes(), "image/jpeg")
                elif route == "/latest_sky_raw.jpg":
                    raw_p = data_dir / "latest_sky_raw.jpg"
                    target = raw_p if raw_p.exists() else (data_dir / "latest_sky_debug.jpg")
                    self.send_body(target.read_bytes(), "image/jpeg")
                elif route == "/":
                    self.send_body(Path(__file__).with_name("dashboard.html").read_bytes(), "text/html; charset=utf-8")
                else:
                    m = SAFE_MEDIA_RE.match(route)
                    if m:
                        kind, day_s, fname = m.groups()
                        p = data_dir / kind / day_s / fname
                        if p.is_file():
                            self.send_body(p.read_bytes(), "image/jpeg")
                            return
                    self.send_body(b'{"error":"not found"}', "application/json", 404)
            except FileNotFoundError:
                self.send_body(b'{"error":"waiting for first observation"}', "application/json", 503)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="sky-dashboard", daemon=True).start()
    logging.getLogger("sky-vision").info("Dashboard listening on :%d", server.server_port)
    return server
