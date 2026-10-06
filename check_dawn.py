#!/usr/bin/env python3
"""Compare dawn rendering on real MTG inputs; no tiles are published.

Run `python3 check_dawn.py`. Cached WCS crops, a contact sheet, and numerical
results go to results/dawn. The colouriser below mirrors the app pixel math.
"Before" reproduces the settings of the reported 2026-10-04 screenshots
(linear MTG gray, frame-time sun, 0.2 floor, re-stretched haze, 0.10–0.30
app ramp); "After" uses the current publisher and app code, whose visible
weight rises linearly in solar time over 2.5 h from cos(sza) 0.05 (or to
local noon when that comes first).
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import io
import json
from pathlib import Path
import urllib.parse
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

import mosaic_v2 as m

OUTPUT = Path(__file__).resolve().parent / "results" / "dawn"
TIMES = [dt.datetime(2026, 10, 3, 6, 50, tzinfo=m.UTC) + dt.timedelta(minutes=10 * i) for i in range(6)]
LAT, LON = (35, 65), (-25, 15)


def crop(when, kind):
    coverage = m.WCS_SOURCES["MTG"][0 if kind == "ir" else 1]
    path = OUTPUT / f"{when:%H%M}-{kind}.tiff"
    if not path.exists():
        # Both channels become approximately 0.05-degree pixels.
        scale = 0.2 if kind == "ir" else 0.1425
        params = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
                  ("coverageId", coverage), ("subset", f"Lat({LAT[0]},{LAT[1]})"),
                  ("subset", f"Long({LON[0]},{LON[1]})"),
                  ("subset", f'time("{when:%Y-%m-%dT%H:%M}:00.000Z")'),
                  ("format", "image/tiff"), ("scaleFactor", str(scale))]
        with urllib.request.urlopen(m.WCS_URL + "?" + urllib.parse.urlencode(params), timeout=45) as response:
            body = response.read()
        # Validate before caching an endpoint error.
        with Image.open(io.BytesIO(body)) as image:
            image.load()
        path.write_bytes(body)
    return m.decode_geotiff(path.read_bytes())


# Settings of the reported screenshots, kept here only for comparison.
OLD_MTG_LUT = m.reflectance_code(np.clip((0.86 * np.arange(256, dtype=np.float32) - 15.0) / 255.0, 0.0, 1.0))


def old_haze(normalised, cos_sun):
    base = np.interp(cos_sun, m.HAZE_COS, m.HAZE_BASE)
    return np.maximum(normalised - base, 0) / (1 - base)


VISIBLE_RAMP_HOUR_ANGLE = np.deg2rad(15 * 2.5)  # visibleRampHourAngle


def daylight(lon, lat, when):
    """ForecastMapSatelliteColouriser.daylight: linear in solar time over
    2.5 h from cos(sza) 0.05, or to local noon when that comes first."""
    declination, equation_of_time, hours = m.solar_terms(when)
    radians = np.deg2rad((hours * 60 + equation_of_time + 4 * lon) / 4 - 180)
    hour_angle = np.abs(np.remainder(radians + np.pi, 2 * np.pi) - np.pi)
    a = np.sin(np.deg2rad(lat)) * np.sin(declination)
    b = np.cos(np.deg2rad(lat)) * np.cos(declination)
    onset = np.arccos(np.clip((.05 - a) / b, -1, 1))
    span = np.minimum(onset, VISIBLE_RAMP_HOUR_ANGLE)
    return np.where(onset > 0, np.clip((onset - hour_angle) / np.maximum(span, 1e-6), 0, 1), 0)


def colourise(ir, vis, day):
    """Straight grayscale/alpha using the Swift colouriser's equations."""
    bt = np.where(ir >= 2, m.ir_temperature(ir), m.IR_WARM_K)
    ia = m.smoothstep(290, 240, bt)
    ig = np.clip((300 - bt) / 100, 0, 1) * .55 + .45
    r = m.reflectance_from_code(vis)
    va = m.smoothstep(.18, .55, r)
    vg = np.clip(.55 + .6 * r, 0, 1)
    day = np.where(vis >= 2, day, 0)
    da = np.maximum(va, ia)
    alpha = day * da + (1 - day) * ia
    gray = (day * da * np.maximum(vg, ig) + (1 - day) * ia * ig) / np.maximum(alpha, 1e-6)
    gray = np.where(alpha > 0, gray, 0)
    return np.rint(gray * 255).astype(np.uint8), np.rint(alpha * 235).astype(np.uint8)


def render(variant, raw, ir, lon, lat, when):
    frame_sun = m.cos_solar_zenith(lon, lat, when)
    if variant == "Before":
        reflectance = m.reflectance_from_code(OLD_MTG_LUT[raw])
        normalised = old_haze(reflectance / np.maximum(frame_sun, .2), frame_sun)
        vis = np.where(frame_sun > m.NIGHT_COS_SZA, m.reflectance_code(normalised), 0).astype(np.uint8)
        return colourise(ir, vis, m.smoothstep(.10, .30, frame_sun))
    scan_sun = m.cos_solar_zenith(lon, lat, when, m.scan_offset_minutes(lon, lat, 0.0, "MTG"))
    reflectance = m.reflectance_from_code(m.MTG_VIS_GRAY_LUT[raw])
    normalised = m.remove_low_sun_haze(reflectance / np.maximum(scan_sun, m.VIS_NORMALISATION_FLOOR), scan_sun)
    vis = np.where(scan_sun > m.NIGHT_COS_SZA, m.reflectance_code(normalised), 0).astype(np.uint8)
    return colourise(ir, vis, daylight(lon, lat, when))


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        crops = list(pool.map(lambda job: crop(*job), [(t, k) for t in TIMES for k in ("ir", "vis")]))
    variants = ("Before", "After")
    fig, axes = plt.subplots(2, 6, figsize=(18, 7.5), layout="constrained")
    reports = {}
    for row, label in enumerate(variants):
        alphas, images = [], []
        for col, when in enumerate(TIMES):
            ir_crop, vis_crop = crops[col * 2: col * 2 + 2]
            raw, lon0, lat0, dx, dy = vis_crop
            lon, lat = np.meshgrid(lon0 + (np.arange(raw.shape[1]) + .5) * dx,
                                   lat0 - (np.arange(raw.shape[0]) + .5) * dy)
            ir_raw, ilon, ilat, idx, idy = ir_crop
            # Match grids using the same validity-normalised sampler as production.
            ir_layer = m.NativeLayer(m.WCS_IR_LUTS["MTG"][ir_raw], None, ilon, ilat, idx, idy)
            r, c = ir_layer.fractional_index(lon, lat)
            ir, valid = m.bilinear_sample(ir_layer.codes, r, c)
            ir = np.where(valid > 0, np.rint(ir), 0).astype(np.uint8)
            gray, alpha = render(label, raw, ir, lon, lat, when)
            alphas.append(alpha.astype(float))
            # Neutral dark background isolates changes in the cloud overlay.
            image = gray * (alpha / 255) + 20 * (1 - alpha / 255)
            images.append(image)
            axes[row, col].imshow(image, cmap="gray", vmin=0, vmax=255, extent=[LON[0], LON[1], LAT[0], LAT[1]])
            axes[row, col].set_title(f"{when + dt.timedelta(hours=2):%H:%M} Zurich")
            if col == 0:
                axes[row, col].set_ylabel(label, fontsize=14)
            axes[row, col].set_xticks([-20, 0]); axes[row, col].set_yticks([40, 60])
        delta = np.abs(np.diff(alphas, axis=0))
        drift = np.diff(images, axis=0)
        reports[label] = {
            "mean_opacity_change_per_10_minutes": round(float(delta.mean()), 3),
            "p95_opacity_change_per_10_minutes": round(float(np.percentile(delta, 95)), 3),
            "mean_brightening_per_10_minutes": [round(float(d.mean()), 2) for d in drift],
            "opaque_fraction_by_frame": [round(float((a >= 180).mean()), 4) for a in alphas],
        }
    fig.suptitle("Europe dawn: actual MTG crops, 3 October 2026\nCloud overlay on a neutral background; no basemap or tile compression", fontsize=16)
    fig.savefig(OUTPUT / "comparison.png", dpi=140)
    plt.close(fig)
    (OUTPUT / "metrics.json").write_text(json.dumps(reports, indent=2) + "\n")
    print(json.dumps(reports, indent=2))
    print(OUTPUT / "comparison.png")


if __name__ == "__main__":
    main()
