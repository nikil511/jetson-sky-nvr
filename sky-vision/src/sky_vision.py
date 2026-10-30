#!/usr/bin/env python3
"""
Sky Computer-Vision Laboratory Service (sky-vision)
- Subscribes to RTSP camera streams (Frigate go2rtc restream or direct camera RTSP)
- Integrates live roof telemetry from WeatherXM WG1200 gateway (configured via WXM_STATUS_URL)
- Computes Lifted Condensation Level (LCL) cloud base ceiling (m AGL), physical cloud speed (m/s),
  and daytime solar clear-sky index / cloud opacity
- Uses Athens astronomical solar elevation to switch Day vs. Night mode (immune to camera Night Mode AGC)
- Uses a custom skyline polygon ROI mask to exclude Athens rooftops, antennas, and hillside lights
- Exports timestamped JSON observations and annotated debug images with ring-buffer cap
- Publishes observations and Home Assistant MQTT Auto-Discovery payloads
"""

import datetime
import json
import logging
import math
import os
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
import paho.mqtt.client as mqtt

from .web import start_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sky-vision")

ATHENS_LAT = float(os.environ.get("SKY_LAT", "37.9838"))
ATHENS_LON = float(os.environ.get("SKY_LON", "23.7275"))
CAMERA_HFOV_DEG = float(os.environ.get("SKY_CAM_HFOV_DEG", "90.0"))


def is_daytime_athens(
    now_utc: Optional[datetime.datetime] = None,
    lat_deg: float = ATHENS_LAT,
    lon_deg: float = ATHENS_LON,
) -> Tuple[bool, float]:
    """
    Compute solar elevation angle (deg) for central Athens from UTC time.
    Returns (is_daytime, sun_elevation_deg).
    Daytime is True when solar elevation > -1.0 deg (sun above or at horizon).
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=datetime.timezone.utc)
    else:
        now_utc = now_utc.astimezone(datetime.timezone.utc)

    day_of_year = now_utc.timetuple().tm_yday
    utc_hours = now_utc.hour + now_utc.minute / 60.0 + now_utc.second / 3600.0

    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (utc_hours - 12.0) / 24.0)

    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.00148 * math.sin(3.0 * gamma)
    )

    time_offset = eqtime + 4.0 * lon_deg
    tst = utc_hours * 60.0 + time_offset
    ha_deg = (tst / 4.0) - 180.0
    ha_rad = math.radians(ha_deg)
    lat_rad = math.radians(lat_deg)

    sin_el = math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(decl) * math.cos(ha_rad)
    sin_el = max(-1.0, min(1.0, sin_el))
    sun_el_deg = math.degrees(math.asin(sin_el))
    return (sun_el_deg > -1.0), round(sun_el_deg, 1)


def compute_dewpoint_and_lcl(temp_c: float, humidity_pct: float) -> Tuple[float, int]:
    """
    Compute dewpoint (°C) via Magnus formula and Lifted Condensation Level (LCL, meters AGL).
    """
    rh = max(1.0, min(100.0, float(humidity_pct)))
    t = float(temp_c)
    a, b = 17.625, 243.04
    gamma = (a * t / (b + t)) + math.log(rh / 100.0)
    dew_c = (b * gamma) / (a - gamma)
    lcl_m = int(round(max(50.0, 125.0 * max(0.0, t - dew_c))))
    return round(dew_c, 2), lcl_m


def fetch_wxm_roof_weather(status_url: str, timeout_sec: float = 3.0) -> Optional[Dict[str, Any]]:
    """
    Fetch live weather telemetry from local WeatherXM WG1200 gateway (/status).
    """
    if not status_url:
        return None
    try:
        req = urllib.request.Request(status_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        w = data.get("weather") if isinstance(data.get("weather"), dict) else data
        if not w or not w.get("has_weather", True):
            return None
        temp_c = float(w["temp"])
        humidity = float(w["humidity"])
        dew_c, lcl_m = compute_dewpoint_and_lcl(temp_c, humidity)
        return {
            "station_name": data.get("dev_name", "WeatherXM WG1200"),
            "station_id": data.get("paired_id"),
            "temp_c": round(temp_c, 2),
            "feels_like_c": round(float(w.get("feels_like", temp_c)), 2),
            "dew_point_c": dew_c,
            "humidity_pct": round(humidity, 1),
            "pressure_hpa": round(float(w.get("pressure", 1013.25)), 2),
            "wind_speed_m_s": round(float(w.get("wind_speed", 0.0)), 2),
            "wind_gust_m_s": round(float(w.get("wind_gust", 0.0)), 2),
            "wind_dir_deg": round(float(w.get("wind_dir_deg", 0.0)), 1),
            "wind_dir_cardinal": str(w.get("wind_dir_cardinal", "")),
            "precip_rate_mm_h": round(float(w.get("precip_rate", 0.0)), 2),
            "precip_daily_mm": round(float(w.get("precip_daily", 0.0)), 2),
            "uv_index": int(w.get("uv", 0)),
            "irradiance_w_m2": round(float(w.get("irradiance", 0.0)), 1),
            "lcl_ceiling_m": lcl_m,
        }
    except Exception as exc:
        logger.warning("Failed to fetch WeatherXM roof telemetry from %s: %s", status_url, exc)
        return None


def estimate_ceiling_and_cloud_physics(
    cloud_metrics: Dict[str, Any],
    flow_metrics: Dict[str, Any],
    roof_weather: Optional[Dict[str, Any]],
    sun_elevation_deg: float,
    frame_width: int = 2560,
) -> Dict[str, Any]:
    """
    Fuse camera cloud segmentation + optical flow + roof weather station (T, RH, wind, irradiance)
    to estimate cloud base height (ceiling in meters AGL), cloud layer regime, and physical cloud speed (m/s).
    """
    cf = float(cloud_metrics.get("cloud_fraction", 0.0))
    flow_px_s = float(flow_metrics.get("flow_speed_px_s", 0.0))

    if roof_weather is None:
        return {
            "lcl_ceiling_m": None,
            "estimated_ceiling_m": None,
            "cloud_layer": "none" if cf < 0.05 else "unknown",
            "cloud_speed_m_s": None,
            "clear_sky_index": None,
        }

    lcl_m = int(roof_weather["lcl_ceiling_m"])
    clear_sky_idx: Optional[float] = None
    if sun_elevation_deg > 5.0:
        sin_el = math.sin(math.radians(sun_elevation_deg))
        # Haurwitz clear-sky global horizontal irradiance model (W/m^2)
        clear_ghi = 1098.0 * sin_el * math.exp(-0.059 / max(0.08, sin_el))
        if clear_ghi > 25.0:
            clear_sky_idx = round(min(1.5, float(roof_weather["irradiance_w_m2"]) / clear_ghi), 2)

    if cf < 0.05:
        return {
            "lcl_ceiling_m": lcl_m,
            "estimated_ceiling_m": None,
            "cloud_layer": "clear",
            "cloud_speed_m_s": 0.0,
            "clear_sky_index": clear_sky_idx,
        }

    # Determine whether observed clouds are boundary-layer (near LCL) or higher mid-level deck:
    # Low clouds have stronger urban night luminance or higher daytime optical flow / lower clear-sky transmittance.
    is_day = bool(cloud_metrics.get("is_daytime", False))
    mean_luma = float(cloud_metrics.get("mean_sky_luma", 90.0))
    mean_rbr = float(cloud_metrics.get("mean_rbr", -0.04))

    if not is_day:
        # At night, warmer/brighter clouds are near LCL; cooler/dimmer clouds sit higher above the urban dome
        if mean_rbr > -0.025 or mean_luma > 108.0:
            est_ceiling_m = lcl_m
            layer = "low" if est_ceiling_m < 2000 else "mid"
        else:
            # Scattered/few clouds slightly above boundary layer LCL
            est_ceiling_m = int(round(max(lcl_m * 1.25, lcl_m + 250)))
            layer = "low" if est_ceiling_m < 2000 else "mid"
    else:
        if clear_sky_idx is not None and clear_sky_idx > 0.85 and cf > 0.4:
            # High translucent cloud layer (cirrus/altostratus) letting >85% solar radiation through
            est_ceiling_m = max(3500, lcl_m * 2)
            layer = "high"
        else:
            est_ceiling_m = lcl_m
            layer = "low" if est_ceiling_m < 2000 else "mid"

    # Convert angular flow speed (px/s) into physical speed (m/s) at est_ceiling_m:
    # rad_per_px = radians(HFOV) / frame_width
    rad_per_px = math.radians(CAMERA_HFOV_DEG) / float(max(320, frame_width))
    # Account for typical ~35 deg elevation look-angle slant range factor (~1.4x vertical height)
    slant_range_m = est_ceiling_m * 1.4
    cloud_speed_m_s = round(flow_px_s * rad_per_px * slant_range_m, 2)

    return {
        "lcl_ceiling_m": lcl_m,
        "estimated_ceiling_m": est_ceiling_m,
        "cloud_layer": layer,
        "cloud_speed_m_s": cloud_speed_m_s,
        "clear_sky_index": clear_sky_idx,
    }


def build_sky_roi_mask(
    height: int,
    width: int,
    horizon_y_ratio: float = 0.75,
    top_osd_y_ratio: float = 0.01,
    left_hill_y_ratio: float = 0.67,
    right_hill_y_ratio: float = 0.65,
) -> np.ndarray:
    """
    Create a binary 7-point polygon mask (uint8, 255=sky, 0=excluded) tailored to the raised Athens sky camera:
    - Left hillside buildings (x=0.00..0.25): slopes from y=0.67 down to y=0.75
    - Center skyline (x=0.25..0.73): extends down to y=0.75 above central rooftops
    - Right antenna pole, hillside floodlights & anemometer (x=0.78..1.00): steps up to y=0.65
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    y_top = max(0, min(height - 1, int(height * top_osd_y_ratio)))
    y_center = max(y_top + 1, min(height, int(height * horizon_y_ratio)))
    y_left = max(y_top + 1, min(y_center, int(height * left_hill_y_ratio)))
    y_right = max(y_top + 1, min(y_center, int(height * right_hill_y_ratio)))

    poly = np.array(
        [
            [0, y_top],
            [width - 1, y_top],
            [width - 1, y_right],
            [int(width * 0.78), y_right],
            [int(width * 0.73), y_center],
            [int(width * 0.25), y_center],
            [0, y_left],
        ],
        dtype=np.int32,
    )
    cv2.fillPoly(mask, [poly], 255)
    return mask


def compute_cloud_mask_and_fraction(
    frame_bgr: np.ndarray,
    sky_mask: Optional[np.ndarray] = None,
    is_daytime: Optional[bool] = None,
) -> Tuple[np.ndarray, Dict[str, float]]:
    h, w = frame_bgr.shape[:2]
    if sky_mask is None:
        sky_mask = build_sky_roi_mask(h, w)

    sky_pixels = int(np.count_nonzero(sky_mask))
    if sky_pixels == 0:
        return np.zeros((h, w), dtype=np.uint8), {
            "cloud_fraction": 0.0,
            "cloud_pct": 0.0,
            "oktas": 0,
            "mean_sky_luma": 0.0,
            "mean_rbr": 0.0,
            "is_daytime": False,
        }

    b = frame_bgr[:, :, 0].astype(np.float32)
    g = frame_bgr[:, :, 1].astype(np.float32)
    r = frame_bgr[:, :, 2].astype(np.float32)

    luma = 0.114 * b + 0.587 * g + 0.299 * r
    mean_luma = float(np.mean(luma[sky_mask > 0]))
    if is_daytime is None:
        is_daytime = mean_luma >= 35.0

    denom = r + b + 1.0
    nrbr = (r - b) / denom
    mean_nrbr = float(np.mean(nrbr[sky_mask > 0]))

    if is_daytime:
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1].astype(np.float32) / 255.0
        val = hsv[:, :, 2].astype(np.float32) / 255.0
        cloud_cond = (
            ((nrbr > -0.08) & (val > 0.30))
            | ((sat < 0.28) & (val > 0.45))
        )
    else:
        luma_filled = luma.copy()
        top_y = max(0, min(h - 1, int(h * 0.01)))
        if top_y > 0:
            luma_filled[:top_y, :] = luma[top_y : top_y + 1, :]
        for x in range(w):
            valid_y = np.where(sky_mask[:, x] > 0)[0]
            if len(valid_y) > 0:
                y_bot = int(valid_y[-1])
                edge_val = float(np.median(luma[max(0, y_bot - 12) : y_bot + 1, x]))
                luma_filled[y_bot + 1 :, x] = edge_val

        ds_w, ds_h = max(16, w // 16), max(16, h // 16)
        dome_small = cv2.resize(luma_filled, (ds_w, ds_h), interpolation=cv2.INTER_AREA)
        dome_blur = cv2.GaussianBlur(dome_small, (0, 0), sigmaX=7.0, sigmaY=7.0)
        dome_2d = cv2.resize(dome_blur, (w, h), interpolation=cv2.INTER_LINEAR)

        local_luma = cv2.GaussianBlur(luma_filled, (0, 0), sigmaX=5.0, sigmaY=5.0)
        cloud_residual = local_luma - dome_2d

        zenith_h = max(1, int(h * 0.22))
        zenith_mask = sky_mask[:zenith_h, :] > 0
        if np.any(zenith_mask):
            zenith_luma = float(np.median(luma[:zenith_h, :][zenith_mask]))
            zenith_nrbr = float(np.median(nrbr[:zenith_h, :][zenith_mask]))
        else:
            zenith_luma = mean_luma
            zenith_nrbr = mean_nrbr

        if zenith_luma > 98.0 and zenith_nrbr > -0.035:
            cloud_cond = (luma > 90.0) & (nrbr > -0.05)
        else:
            cloud_cond = (cloud_residual > 2.4) & (nrbr > -0.095)

    cloud_mask = np.zeros((h, w), dtype=np.uint8)
    cloud_mask[cloud_cond & (sky_mask > 0)] = 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    cloud_mask = cv2.morphologyEx(cloud_mask, cv2.MORPH_OPEN, kernel)
    cloud_mask = cv2.morphologyEx(cloud_mask, cv2.MORPH_CLOSE, kernel)
    cloud_mask[sky_mask == 0] = 0

    cloud_pixels = int(np.count_nonzero(cloud_mask))
    cloud_fraction = round(float(cloud_pixels) / float(sky_pixels), 4)
    cloud_pct = round(cloud_fraction * 100.0, 1)
    oktas = int(round(cloud_fraction * 8.0))

    return cloud_mask, {
        "cloud_fraction": cloud_fraction,
        "cloud_pct": cloud_pct,
        "oktas": oktas,
        "mean_sky_luma": round(mean_luma, 2),
        "mean_rbr": round(mean_nrbr, 4),
        "is_daytime": bool(is_daytime),
    }


def classify_sky_condition(cloud_fraction: float) -> str:
    if cloud_fraction < 0.125:
        return "clear"
    if cloud_fraction < 0.375:
        return "few_clouds"
    if cloud_fraction < 0.625:
        return "scattered_clouds"
    if cloud_fraction < 0.875:
        return "broken_clouds"
    return "overcast"


def compute_sky_optical_flow(
    prev_bgr: np.ndarray,
    curr_bgr: np.ndarray,
    sky_mask: np.ndarray,
    dt_sec: float = 4.0,
    scale: float = 0.5,
    cloud_mask: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, Dict[str, float]]:
    dt = max(0.05, float(dt_sec))
    h, w = prev_bgr.shape[:2]
    sh, sw = max(32, int(h * scale)), max(32, int(w * scale))

    prev_gray = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(prev_bgr, (sw, sh)), cv2.COLOR_BGR2GRAY), (5, 5), 0)
    curr_gray = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(curr_bgr, (sw, sh)), cv2.COLOR_BGR2GRAY), (5, 5), 0)
    smask = cv2.resize(sky_mask, (sw, sh), interpolation=cv2.INTER_NEAREST) > 0

    if cloud_mask is not None:
        cmask = cv2.resize(cloud_mask, (sw, sh), interpolation=cv2.INTER_NEAREST) > 0
        flow_roi = smask & cmask
    else:
        flow_roi = smask

    flow = cv2.calcOpticalFlowFarneback(
        prev_gray,
        curr_gray,
        None,
        pyr_scale=0.5,
        levels=4,
        winsize=25,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    )

    flow[~flow_roi] = 0.0
    vx = flow[:, :, 0][flow_roi] / dt
    vy = flow[:, :, 1][flow_roi] / dt
    mag = np.hypot(vx, vy)

    active = (mag > 0.20) & (mag < 25.0)
    if np.count_nonzero(active) > 25:
        mean_vx = float(np.mean(vx[active]))
        mean_vy = float(np.mean(vy[active]))
        mean_speed = float(np.mean(mag[active])) / scale
        p90_speed = float(np.percentile(mag[active], 90)) / scale
        azimuth_deg = (math.degrees(math.atan2(mean_vx, -mean_vy)) + 360.0) % 360.0
    else:
        mean_vx = 0.0
        mean_vy = 0.0
        mean_speed = 0.0
        p90_speed = 0.0
        azimuth_deg = 0.0

    return flow, {
        "flow_speed_px_s": round(mean_speed, 2),
        "flow_p90_speed_px_s": round(p90_speed, 2),
        "flow_azimuth_deg": round(azimuth_deg, 1),
        "flow_vx": round(mean_vx, 3),
        "flow_vy": round(mean_vy, 3),
    }


def render_debug_overlay(
    frame_bgr: np.ndarray,
    sky_mask: np.ndarray,
    cloud_mask: np.ndarray,
    flow: Optional[np.ndarray],
    observation: Dict,
    out_width: int = 1280,
) -> np.ndarray:
    h, w = frame_bgr.shape[:2]
    scale = out_width / float(w)
    out_h = int(h * scale)

    canvas = cv2.resize(frame_bgr, (out_width, out_h))
    smask_s = cv2.resize(sky_mask, (out_width, out_h), interpolation=cv2.INTER_NEAREST)
    cmask_s = cv2.resize(cloud_mask, (out_width, out_h), interpolation=cv2.INTER_NEAREST)

    overlay = canvas.copy()
    overlay[cmask_s > 0] = (255, 220, 0)
    canvas = cv2.addWeighted(canvas, 0.78, overlay, 0.22, 0)

    contours, _ = cv2.findContours(smask_s, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(canvas, contours, -1, (0, 255, 255), 2)

    if flow is not None:
        fh, fw = flow.shape[:2]
        step = max(8, fw // 24)
        for y in range(step // 2, fh, step):
            for x in range(step // 2, fw, step):
                dx, dy = flow[y, x]
                if math.hypot(dx, dy) < 0.25:
                    continue
                px = int(x * out_width / fw)
                py = int(y * out_h / fh)
                cy = min(out_h - 1, py)
                cx = min(out_width - 1, px)
                if smask_s[cy, cx] == 0 or cmask_s[cy, cx] == 0:
                    continue
                ex = int(px + dx * 6.0)
                ey = int(py + dy * 6.0)
                cv2.arrowedLine(canvas, (px, py), (ex, ey), (0, 80, 255), 2, tipLength=0.3)

    banner_h = 54
    cv2.rectangle(canvas, (0, out_h - banner_h), (out_width, out_h), (15, 15, 15), -1)
    mode_lbl = "DAY" if observation.get("is_daytime") else "NIGHT"
    ceil_m = observation.get("estimated_ceiling_m") or observation.get("lcl_ceiling_m")
    ceil_str = f" | Ceil: ~{ceil_m}m" if ceil_m else ""
    summary = (
        f"Athens Sky [{mode_lbl}] | {observation['timestamp']} | "
        f"Clouds: {observation['cloud_pct']:.1f}% ({observation['oktas']}/8, {observation['sky_condition']})"
        f"{ceil_str} | Flow: {observation['flow_speed_px_s']:.1f} px/s"
    )
    cv2.putText(
        canvas,
        summary,
        (16, out_h - 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.53,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return canvas


def prune_storage_directory(base_dir: Path, max_mb: int = 1500) -> None:
    if not base_dir.exists():
        return
    files = [p for p in base_dir.rglob("*") if p.is_file() and not p.name.startswith("latest_")]
    total_bytes = sum(p.stat().st_size for p in files)
    limit_bytes = max_mb * 1024 * 1024
    if total_bytes <= limit_bytes:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files:
        try:
            sz = p.stat().st_size
            p.unlink()
            total_bytes -= sz
            if total_bytes <= int(limit_bytes * 0.85):
                break
        except OSError:
            pass


def capture_frame_pair(rtsp_url: str, dt_target: float = 1.0) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], float]:
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return None, None, 0.0
    try:
        for _ in range(3):
            cap.grab()
        ok1, frame1 = cap.read()
        t1 = time.monotonic()
        if not ok1 or frame1 is None:
            return None, None, 0.0

        while (time.monotonic() - t1) < dt_target:
            if not cap.grab():
                break
            time.sleep(0.02)
        ok2, frame2 = cap.read()
        t2 = time.monotonic()
        if not ok2 or frame2 is None:
            return frame1, frame1.copy(), 0.05
        return frame1, frame2, max(0.05, t2 - t1)
    finally:
        cap.release()


def publish_ha_discovery(client: mqtt.Client, prefix: str = "sky_vision/athens") -> None:
    device_info = {
        "identifiers": ["jetson_nx_sky_vision_athens"],
        "name": "Athens Sky Vision Lab",
        "manufacturer": "Manobot R&D",
        "model": "Jetson Xavier NX Sky-CV",
        "sw_version": "1.3.0",
    }
    sensors = [
        {
            "id": "cloud_cover",
            "name": "Athens Sky Cloud Cover",
            "val_tpl": "{{ value_json.cloud_pct }}",
            "unit": "%",
            "icon": "mdi:weather-cloudy",
        },
        {
            "id": "cloud_oktas",
            "name": "Athens Sky Cloud Oktas",
            "val_tpl": "{{ value_json.oktas }}",
            "unit": "oktas",
            "icon": "mdi:cloud-outline",
        },
        {
            "id": "sky_condition",
            "name": "Athens Sky Condition",
            "val_tpl": "{{ value_json.sky_condition }}",
            "unit": None,
            "icon": "mdi:weather-partly-cloudy",
        },
        {
            "id": "estimated_ceiling",
            "name": "Athens Estimated Cloud Ceiling",
            "val_tpl": "{{ value_json.estimated_ceiling_m | default(value_json.lcl_ceiling_m, true) }}",
            "unit": "m",
            "icon": "mdi:cloud-arrow-up",
        },
        {
            "id": "cloud_speed_ms",
            "name": "Athens Estimated Cloud Speed",
            "val_tpl": "{{ value_json.cloud_speed_m_s }}",
            "unit": "m/s",
            "icon": "mdi:weather-windy",
        },
        {
            "id": "flow_speed",
            "name": "Athens Cloud Optical Flow Speed",
            "val_tpl": "{{ value_json.flow_speed_px_s }}",
            "unit": "px/s",
            "icon": "mdi:weather-windy",
        },
        {
            "id": "flow_azimuth",
            "name": "Athens Cloud Optical Flow Azimuth",
            "val_tpl": "{{ value_json.flow_azimuth_deg }}",
            "unit": "°",
            "icon": "mdi:compass-outline",
        },
    ]
    for s in sensors:
        cfg_topic = f"homeassistant/sensor/sky_vision_athens/{s['id']}/config"
        payload = {
            "name": s["name"],
            "unique_id": f"sky_vision_athens_{s['id']}",
            "state_topic": f"{prefix}/state",
            "value_template": s["val_tpl"],
            "icon": s["icon"],
            "device": device_info,
        }
        if s["unit"]:
            payload["unit_of_measurement"] = s["unit"]
        client.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)


def run_service() -> None:
    primary_rtsp = os.environ.get("SKY_CAM_RTSP_URL", "rtsp://frigate:8554/athens_sky")
    fallback_rtsp = os.environ.get("SKY_CAM_RTSP_MAIN", primary_rtsp)
    wxm_status_url = os.environ.get("WXM_STATUS_URL", "")
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    mqtt_topic = os.environ.get("SKY_MQTT_TOPIC", "sky_vision/athens")
    interval_sec = int(os.environ.get("EXTRACT_INTERVAL_SEC", "300"))
    max_storage_mb = int(os.environ.get("MAX_STORAGE_MB", "1500"))
    data_dir = Path(os.environ.get("SKY_DATA_DIR", "/data"))

    flow_dt_sec = float(os.environ.get("FLOW_DT_SEC", "4.0"))
    obs_dir = data_dir / "observations"
    dbg_dir = data_dir / "debug"
    raw_dir = data_dir / "raw"
    obs_dir.mkdir(parents=True, exist_ok=True)
    dbg_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    start_server(data_dir, int(os.environ.get("SKY_HTTP_PORT", "8080")))

    client = mqtt.Client(client_id="sky-vision-lab")
    mqtt_connected = False
    try:
        client.connect(mqtt_host, mqtt_port, keepalive=60)
        client.loop_start()
        mqtt_connected = True
        publish_ha_discovery(client, mqtt_topic)
        logger.info("Connected to MQTT broker at %s:%d", mqtt_host, mqtt_port)
    except Exception as exc:
        logger.warning("Initial MQTT connection failed (%s); will retry on publish", exc)

    sky_mask_cache: Optional[np.ndarray] = None

    while True:
        cycle_start = time.monotonic()
        f1, f2, dt = capture_frame_pair(primary_rtsp, dt_target=flow_dt_sec)
        source_used = "frigate_restream"
        if f1 is None and fallback_rtsp != primary_rtsp:
            logger.info("Primary restream unavailable, falling back to direct camera RTSP")
            f1, f2, dt = capture_frame_pair(fallback_rtsp, dt_target=flow_dt_sec)
            source_used = "direct_rtsp"

        if f1 is not None and f2 is not None:
            h, w = f1.shape[:2]
            if sky_mask_cache is None or sky_mask_cache.shape[:2] != (h, w):
                sky_mask_cache = build_sky_roi_mask(h, w)

            now_utc = datetime.datetime.now(datetime.timezone.utc)
            is_day, sun_el_deg = is_daytime_athens(now_utc)

            cloud_mask, cloud_metrics = compute_cloud_mask_and_fraction(
                f2, sky_mask_cache, is_daytime=is_day
            )
            flow, flow_metrics = compute_sky_optical_flow(
                f1, f2, sky_mask_cache, dt_sec=dt, cloud_mask=cloud_mask
            )
            roof_weather = fetch_wxm_roof_weather(wxm_status_url)
            ceiling_metrics = estimate_ceiling_and_cloud_physics(
                cloud_metrics, flow_metrics, roof_weather, sun_el_deg, frame_width=w
            )

            ts_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            day_str = now_utc.strftime("%Y-%m-%d")
            stamp_str = now_utc.strftime("%Y%m%d_%H%M%S")

            observation = {
                "timestamp": ts_iso,
                "camera": "athens_sky",
                "source": source_used,
                "resolution": f"{w}x{h}",
                "sun_elevation_deg": sun_el_deg,
                "sky_condition": classify_sky_condition(cloud_metrics["cloud_fraction"]),
                **cloud_metrics,
                **flow_metrics,
                **ceiling_metrics,
                "roof_weather": roof_weather,
            }

            day_obs_dir = obs_dir / day_str
            day_obs_dir.mkdir(parents=True, exist_ok=True)
            obs_file = day_obs_dir / f"obs_{stamp_str}.json"
            latest_obs_file = data_dir / "latest_observation.json"
            obs_json = json.dumps(observation, indent=2)
            obs_file.write_text(obs_json)
            pending_obs = data_dir / ".latest_observation.tmp"
            pending_obs.write_text(obs_json)

            debug_img = render_debug_overlay(f2, sky_mask_cache, cloud_mask, flow, observation)
            out_h, out_w = debug_img.shape[:2]
            raw_img = cv2.resize(f2, (out_w, out_h))
            day_dbg_dir = dbg_dir / day_str
            day_raw_dir = raw_dir / day_str
            day_dbg_dir.mkdir(parents=True, exist_ok=True)
            day_raw_dir.mkdir(parents=True, exist_ok=True)
            dbg_file = day_dbg_dir / f"sky_{stamp_str}.jpg"
            raw_file = day_raw_dir / f"raw_{stamp_str}.jpg"
            latest_dbg_file = data_dir / "latest_sky_debug.jpg"
            latest_raw_file = data_dir / "latest_sky_raw.jpg"
            cv2.imwrite(str(dbg_file), debug_img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            cv2.imwrite(str(raw_file), raw_img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            pending_dbg = data_dir / ".latest_debug.tmp"
            pending_raw = data_dir / ".latest_raw.tmp"
            shutil.copyfile(dbg_file, pending_dbg)
            shutil.copyfile(raw_file, pending_raw)
            pending_dbg.replace(latest_dbg_file)
            pending_raw.replace(latest_raw_file)
            pending_obs.replace(latest_obs_file)

            prune_storage_directory(data_dir, max_mb=max_storage_mb)

            try:
                if not mqtt_connected:
                    client.connect(mqtt_host, mqtt_port, keepalive=60)
                    client.loop_start()
                    mqtt_connected = True
                    publish_ha_discovery(client, mqtt_topic)
                client.publish(f"{mqtt_topic}/state", json.dumps(observation), qos=1, retain=True)
                ok_jpg, buf = cv2.imencode(".jpg", debug_img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok_jpg:
                    client.publish(f"{mqtt_topic}/debug_image", buf.tobytes(), qos=0, retain=True)
            except Exception as exc:
                logger.warning("MQTT publish error: %s", exc)
                mqtt_connected = False

            logger.info(
                "Observation saved [%s, sun=%.1f deg]: clouds=%.1f%% (%d/8 oktas, %s), ceiling=%s m (LCL=%s m), flow=%.1f px/s (%s m/s)",
                "DAY" if is_day else "NIGHT",
                sun_el_deg,
                observation["cloud_pct"],
                observation["oktas"],
                observation["sky_condition"],
                observation.get("estimated_ceiling_m"),
                observation.get("lcl_ceiling_m"),
                observation["flow_speed_px_s"],
                observation.get("cloud_speed_m_s"),
            )
        else:
            logger.warning("Failed to capture RTSP frame pair; retrying next cycle")

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1.0, interval_sec - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run_service()
