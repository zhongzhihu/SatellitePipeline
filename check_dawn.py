#!/usr/bin/env python3
"""Compare dawn rendering on real MTG inputs; no tiles are published.

Run `python3 check_dawn.py`. Cached WCS crops, a contact sheet, and numerical
results go to results/dawn. The colouriser below mirrors the app pixel math;
the baseline preserves the settings used in the reported screenshots.
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


def colourise(ir, vis, sun, transition, preserve_ir):
    """Straight grayscale/alpha using the Swift colouriser's equations."""
    bt = np.where(ir >= 2, m.ir_temperature(ir), m.IR_WARM_K)
    ia = m.smoothstep(290, 240, bt)
    ig = np.clip((300 - bt) / 100, 0, 1) * .55 + .45
    r = m.reflectance_from_code(vis)
    va = m.smoothstep(.18, .55, r)
    vg = np.clip(.55 + .6 * r, 0, 1)
    day = np.where(vis >= 2, m.smoothstep(*transition, sun), 0)
    da = np.maximum(va, ia if preserve_ir else .9 * ia)
    alpha = day * da + (1 - day) * ia
    gray = (day * da * np.maximum(vg, ig) + (1 - day) * ia * ig) / np.maximum(alpha, 1e-6)
    gray = np.where(alpha > 0, gray, 0)
    return np.rint(gray * 255).astype(np.uint8), np.rint(alpha * 235).astype(np.uint8)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        crops = list(pool.map(lambda job: crop(*job), [(t, k) for t in TIMES for k in ("ir", "vis")]))
    variants = [("Current", .2, (-.02, .12), False),
                ("Wider dawn blend", .2, (.1, .3), True),
                ("Blend + normalization", .1, (.1, .3), True)]
    fig, axes = plt.subplots(3, 6, figsize=(18, 11), layout="constrained")
    reports = {}
    selected_images = {}
    for row, (label, floor, transition, preserve) in enumerate(variants):
        alphas = []
        for col, when in enumerate(TIMES):
            ir_crop, vis_crop = crops[col * 2: col * 2 + 2]
            raw, lon0, lat0, dx, dy = vis_crop
            lon, lat = np.meshgrid(lon0 + (np.arange(raw.shape[1]) + .5) * dx,
                                   lat0 - (np.arange(raw.shape[0]) + .5) * dy)
            sun = m.cos_solar_zenith(lon, lat, when)
            ir_raw, ilon, ilat, idx, idy = ir_crop
            # Match grids using the same validity-normalised sampler as production.
            ir_layer = m.NativeLayer(m.WCS_IR_LUTS["MTG"][ir_raw], None, ilon, ilat, idx, idy)
            r, c = ir_layer.fractional_index(lon, lat)
            ir, valid = m.bilinear_sample(ir_layer.codes, r, c)
            ir = np.where(valid > 0, np.rint(ir), 0).astype(np.uint8)
            reflectance = m.reflectance_from_code(m.VIS_GRAY_LUT[raw])
            normalized = m.remove_low_sun_haze(reflectance / np.maximum(sun, floor), sun)
            vis = np.where(sun > m.NIGHT_COS_SZA, m.reflectance_code(normalized), 0).astype(np.uint8)
            gray, alpha = colourise(ir, vis, sun, transition, preserve)
            alphas.append(alpha.astype(float))
            # Neutral dark background isolates changes in the cloud overlay.
            image = gray * (alpha / 255) + 20 * (1 - alpha / 255)
            if row < 2 and col in (0, 2, 5):
                selected_images[row, col] = image
            axes[row, col].imshow(image, cmap="gray", vmin=0, vmax=255, extent=[LON[0], LON[1], LAT[0], LAT[1]])
            axes[row, col].set_title(f"{when + dt.timedelta(hours=2):%H:%M} Zurich")
            if col == 0:
                axes[row, col].set_ylabel(label)
            axes[row, col].set_xticks([-20, 0]); axes[row, col].set_yticks([40, 60])
        delta = np.abs(np.diff(alphas, axis=0))
        reports[label] = {
            "mean_opacity_change_per_10_minutes": round(float(delta.mean()), 3),
            "p95_opacity_change_per_10_minutes": round(float(np.percentile(delta, 95)), 3),
            "opaque_fraction_by_frame": [round(float((a >= 180).mean()), 4) for a in alphas],
        }
    fig.suptitle("Europe dawn: actual MTG crops, 3 October 2026\nCloud overlay on a neutral background; no basemap or tile compression", fontsize=16)
    fig.savefig(OUTPUT / "comparison.png", dpi=140)
    plt.close(fig)
    fig, axes = plt.subplots(2, 3, figsize=(12, 6), layout="constrained")
    for row in range(2):
        for column, frame in enumerate((0, 2, 5)):
            axes[row, column].imshow(selected_images[row, frame], cmap="gray", vmin=0, vmax=255,
                                      extent=[LON[0], LON[1], LAT[0], LAT[1]])
            axes[row, column].set_title(f"{TIMES[frame] + dt.timedelta(hours=2):%H:%M} Zurich")
            axes[row, column].set_xticks([]); axes[row, column].set_yticks([])
            if column == 0:
                axes[row, column].set_ylabel("Before" if row == 0 else "After", fontsize=14)
    fig.suptitle("Europe sunrise: before / after\nActual MTG inputs, 3 October 2026; cloud overlay on a neutral background", fontsize=14)
    fig.savefig(OUTPUT / "before-after.png", dpi=150)
    plt.close(fig)
    (OUTPUT / "metrics.json").write_text(json.dumps(reports, indent=2) + "\n")
    print(json.dumps(reports, indent=2))
    print(OUTPUT / "comparison.png")


if __name__ == "__main__":
    main()
