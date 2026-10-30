import datetime
import numpy as np
from src.sky_vision import (
    build_sky_roi_mask,
    classify_sky_condition,
    compute_cloud_mask_and_fraction,
    compute_sky_optical_flow,
    is_daytime_athens,
    compute_dewpoint_and_lcl,
    estimate_ceiling_and_cloud_physics,
)


def test_clear_blue_sky_has_low_cloud_fraction():
    # Deep blue sky in BGR: B=210, G=110, R=40
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[:, :] = (210, 110, 40)
    mask = build_sky_roi_mask(360, 640)
    cmask, metrics = compute_cloud_mask_and_fraction(frame, mask, is_daytime=True)
    assert metrics["is_daytime"] is True
    assert metrics["cloud_fraction"] < 0.05
    assert classify_sky_condition(metrics["cloud_fraction"]) == "clear"
    assert np.count_nonzero(cmask) == 0


def test_overcast_white_sky_has_high_cloud_fraction():
    # Bright white/gray cloud deck in BGR: B=215, G=215, R=215
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[:, :] = (215, 215, 215)
    mask = build_sky_roi_mask(360, 640)
    _, metrics = compute_cloud_mask_and_fraction(frame, mask, is_daytime=True)
    assert metrics["is_daytime"] is True
    assert metrics["cloud_fraction"] > 0.95
    assert metrics["oktas"] == 8
    assert classify_sky_condition(metrics["cloud_fraction"]) == "overcast"


def test_athens_solar_day_night_schedule_and_agc_night_sky():
    # Noon UTC (15:00 Athens) vs 20:00 UTC (23:00 Athens) in October
    is_day_noon, el_noon = is_daytime_athens(
        datetime.datetime(2026, 10, 24, 10, 0, tzinfo=datetime.timezone.utc)
    )
    is_day_night, el_night = is_daytime_athens(
        datetime.datetime(2026, 10, 24, 20, 0, tzinfo=datetime.timezone.utc)
    )
    assert is_day_noon is True and el_noon > 20.0
    assert is_day_night is False and el_night < -15.0

    # Simulate AGC-boosted clear night sky (luma ~85, slight blue excess) + bright building roof at bottom
    h, w = 360, 640
    frame = np.full((h, w, 3), (92, 85, 78), dtype=np.uint8)
    frame[int(h * 0.78):, :] = (160, 190, 220)  # bright building lights below skyline
    mask = build_sky_roi_mask(h, w)
    # Verify building zone is excluded from mask
    assert np.count_nonzero(mask[int(h * 0.78):, :]) == 0

    _, night_metrics = compute_cloud_mask_and_fraction(frame, mask, is_daytime=False)
    assert night_metrics["is_daytime"] is False
    assert night_metrics["cloud_fraction"] < 0.05


def test_optical_flow_detects_cloud_translation_and_ignores_buildings():
    h, w = 360, 640
    f1 = np.full((h, w, 3), (200, 110, 45), dtype=np.uint8)
    f2 = f1.copy()
    # Draw synthetic cloud patch inside upper sky ROI moving +12 px right (East, ~90 deg azimuth)
    f1[60:140, 200:320] = (230, 230, 230)
    f2[60:140, 212:332] = (230, 230, 230)
    # Draw fake flickering movement in the building zone (y=240..320) which must be ignored
    f1[290:350, 100:200] = (250, 250, 250)
    f2[290:350, 100:200] = (20, 20, 20)

    mask = build_sky_roi_mask(h, w)
    cmask, _ = compute_cloud_mask_and_fraction(f2, mask, is_daytime=True)
    flow, flow_metrics = compute_sky_optical_flow(f1, f2, mask, dt_sec=1.0, scale=0.5, cloud_mask=cmask)
    assert flow_metrics["flow_speed_px_s"] > 1.0
    assert 45.0 <= flow_metrics["flow_azimuth_deg"] <= 135.0
    # Ensure building zone has zero optical flow
    assert np.all(flow[int(290 * 0.5):, :, :] == 0.0)


def test_lcl_and_ceiling_estimation_from_wxm_roof_station():
    dew_c, lcl_m = compute_dewpoint_and_lcl(18.61, 74.7)
    assert 13.8 <= dew_c <= 14.4
    assert 500 <= lcl_m <= 620
    roof = {
        "lcl_ceiling_m": lcl_m,
        "irradiance_w_m2": 0.0,
    }
    res = estimate_ceiling_and_cloud_physics(
        {"cloud_fraction": 0.25, "is_daytime": False, "mean_sky_luma": 99.0, "mean_rbr": -0.04},
        {"flow_speed_px_s": 2.0},
        roof,
        sun_elevation_deg=-45.0,
        frame_width=2560,
    )
    assert res["lcl_ceiling_m"] == lcl_m
    assert res["estimated_ceiling_m"] >= lcl_m
    assert res["cloud_speed_m_s"] > 0.0
