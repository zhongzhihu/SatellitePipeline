#!/usr/bin/env python3
"""Render geostationary observations into packed infrared and visible tiles."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import gc
import io
import math
import multiprocessing as mp
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path
from time import perf_counter

import numpy as np
from PIL import Image

import pipeline_support as support

UTC = dt.timezone.utc
TILE_SIZE = 1024
MIN_ZOOM = 1
MAX_ZOOM = 5
BLOCK_SIZE = 2048  # z5 pixels per worker block (2×2 tiles)
NODE_STEP = 16  # geometry is computed every 16 px and interpolated
WORLD_PX = TILE_SIZE << MAX_ZOOM

IR_WARM_K = 321.25
IR_COLD_K = 182.75
IR_STEP_K = (IR_WARM_K - IR_COLD_K) / 254.0
VIS_NORMALISATION_FLOOR = 0.08  # cos(sza) floor for reflectance normalisation
NIGHT_COS_SZA = math.cos(math.radians(89.0))
BLUE_OFFSET = 0.08  # Rayleigh path radiance in 0.47 µm vs 0.64 µm reflectance
BLUE_GAIN = 0.92

# smoothstep on cos(view zenith). Data is used out to ~88° zenith so the gaps
# between neighbouring disks close at high latitude; the polar mask below then
# trims the scalloped disk edges to a straight line.
LIMB_FADE = (0.03, 0.2)
SOFTMAX_TAU = 0.02
BOX_EDGE_FADE_PX = 80  # feather clipped WCS boxes over ~80 native pixels

# Straight polar cut, instead of following each disk's circular limb. The
# only place the disks do not reach POLAR_CAP_DEG is the Himawari/IODC gap
# over Siberia (≈74.4° at 93° E), so a notch is cut there, and mirrored in the
# south. Piecewise-linear (longitude, latitude) points; outside them the cap
# applies.
POLAR_CAP_DEG = 75.0
POLAR_NOTCH = ((50.0, 75.0), (62.0, 72.0), (100.0, 72.0), (110.0, 75.0))

PACK_MAGIC = b"WXSP"
PACK_VERSION = 3
PACK_HEADER = struct.Struct("<4sIIII")  # magic, version, min zoom, max zoom, entry count
INDEX_ENTRY = struct.Struct("<II")  # absolute offset, length (0 = empty tile)

# Published in the v2 manifest; the Worker rejects any other descriptor so
# the publisher and Worker use the same scale.
ENCODING_DESCRIPTOR = {
    "format": "avif",
    "layout": "ir-over-vis",
    "tile_width": TILE_SIZE,
    "tile_height": TILE_SIZE * 2,
    "nodata": 0,
    "ir": {"quantity": "brightness_temperature", "unit": "K", "code_min": 1, "code_max": 255,
           "value_at_code_min": IR_WARM_K, "value_at_code_max": IR_COLD_K},
    "vis": {"quantity": "reflectance", "curve": "sqrt", "code_min": 1, "code_max": 255,
            "value_at_code_min": 0, "value_at_code_max": 1},
}

WCS_URL = "https://view.eumetsat.int/geoserver/wcs"
WCS_USER_AGENT = "SatellitePipeline/2.0"
WCS_WORKERS = 2

# Gray-to-temperature calibration anchors for EUMETView products.
MTG_IR_ANCHORS = (
    (15, 207.5), (30, 210.8), (45, 215.8), (60, 220.6), (75, 231.0), (90, 238.5),
    (105, 247.3), (120, 253.8), (135, 256.8), (150, 260.5), (165, 266.1), (180, 271.7),
    (195, 278.2), (210, 283.4), (225, 290.2), (240, 296.6), (255, 312.3),
)
IODC_IR_ANCHORS = (
    (30, 292.3), (45, 288.1), (60, 283.0), (75, 279.0), (90, 275.0), (105, 270.6),
    (120, 266.2), (135, 262.4), (150, 255.6), (165, 251.0), (180, 247.5), (195, 244.2),
    (210, 240.3), (225, 230.6), (240, 222.0), (255, 207.0),
)
FES_IR_ANCHORS = (
    (0, 304.0), (8, 299.5), (16, 297.4), (24, 294.6), (32, 291.7), (40, 288.9),
    (48, 286.8), (56, 284.8), (64, 282.4), (72, 280.7), (80, 278.7), (88, 275.8),
    (96, 273.0), (104, 270.1), (112, 267.3), (120, 264.4), (128, 262.0), (136, 259.1),
    (144, 256.7), (152, 254.2), (160, 251.8), (168, 249.4), (176, 247.3), (184, 244.9),
    (192, 242.0), (200, 238.8), (208, 235.9), (216, 232.6), (224, 229.0), (232, 223.7),
    (240, 219.6), (248, 213.1),
)


# --------------------------------------------------------------------------
# Channel encodings


def ir_code(bt: np.ndarray) -> np.ndarray:
    bt = np.asarray(bt, dtype=np.float32)
    valid = np.isfinite(bt) & (bt > 150.0) & (bt < 350.0)
    code = np.zeros(bt.shape, dtype=np.uint8)
    code[valid] = np.clip(np.rint(1.0 + (IR_WARM_K - bt[valid]) / IR_STEP_K), 1, 255).astype(np.uint8)
    return code


def ir_temperature(code: np.ndarray) -> np.ndarray:
    return IR_WARM_K - (np.asarray(code, dtype=np.float32) - 1.0) * IR_STEP_K


def reflectance_code(reflectance: np.ndarray) -> np.ndarray:
    """Encode reflectance factor (0..1) with a sqrt curve; NaN → 0."""
    r = np.asarray(reflectance, dtype=np.float32)
    valid = np.isfinite(r)
    code = np.zeros(r.shape, dtype=np.uint8)
    code[valid] = np.rint(1.0 + 254.0 * np.sqrt(np.clip(r[valid], 0.0, 1.0))).astype(np.uint8)
    return code


def reflectance_from_code(code: np.ndarray) -> np.ndarray:
    c = np.maximum(np.asarray(code, dtype=np.float32) - 1.0, 0.0) / 254.0
    return c * c


def blue_to_red_reflectance(blue: np.ndarray) -> np.ndarray:
    return (np.asarray(blue, dtype=np.float32) - BLUE_OFFSET) / BLUE_GAIN


def anchor_lut(anchors: tuple[tuple[int, float], ...]) -> np.ndarray:
    """256-entry gray → IR code table, extrapolated linearly past the anchors."""
    gray = np.array([a for a, _ in anchors], dtype=np.float64)
    bt = np.array([b for _, b in anchors], dtype=np.float64)
    x = np.arange(256, dtype=np.float64)
    values = np.interp(x, gray, bt)
    low_slope = (bt[1] - bt[0]) / (gray[1] - gray[0])
    high_slope = (bt[-1] - bt[-2]) / (gray[-1] - gray[-2])
    values = np.where(x < gray[0], bt[0] + (x - gray[0]) * low_slope, values)
    values = np.where(x > gray[-1], bt[-1] + (x - gray[-1]) * high_slope, values)
    return ir_code(values.astype(np.float32))


VIS_GRAY_LUT = reflectance_code(np.arange(256, dtype=np.float32) / 255.0)

# EUMETView's MSG vis006 gray is linear in reflectance, but its MTG vis06_hrfi
# gray is gamma-encoded: dark scenes are lifted and the curve is not a
# straight line. Median collocated MSG 0° gray per MTG gray bin, from WCS scans
# on 2026-10-03 (09:00, 12:00 UTC) and 2026-10-04 (08:00, 08:30 UTC) across
# Europe, the Atlantic and Africa. Below the table the transfer is taken as
# proportional; above it the fitted power law FES ≈ 0.105·MTG^1.406 continues
# from the last point. Decoding MTG with a linear map instead made dawn
# clouds brighten far faster than the sun, and made MTG and MSG frames differ
# by up to 30 gray levels. Zero is WCS nodata and must stay missing.
_MTG_TRANSFER_GRAY = np.arange(23, 204, 6, dtype=np.float64)
_MTG_TRANSFER_FES = np.array([
    9.8, 12.9, 16.2, 19.5, 22.7, 26.1, 29.5, 33.2, 37.7, 43.4, 49.5, 56.0, 63.1, 70.3, 77.5, 84.3,
    90.0, 95.8, 101.2, 106.9, 112.8, 118.9, 125.9, 133.3, 141.5, 147.8, 157.7, 164.5, 172.6, 178.4, 187.1,
])


def _mtg_vis_gray_lut() -> np.ndarray:
    gray = np.arange(256, dtype=np.float64)
    fes = np.interp(gray, _MTG_TRANSFER_GRAY, _MTG_TRANSFER_FES)
    low = gray < _MTG_TRANSFER_GRAY[0]
    fes[low] = gray[low] * _MTG_TRANSFER_FES[0] / _MTG_TRANSFER_GRAY[0]
    high = gray > _MTG_TRANSFER_GRAY[-1]
    fes[high] = _MTG_TRANSFER_FES[-1] * (gray[high] / _MTG_TRANSFER_GRAY[-1]) ** 1.406
    lut = reflectance_code(np.clip(fes / 255.0, 0.0, 1.0).astype(np.float32))
    lut[0] = 0
    return lut


MTG_VIS_GRAY_LUT = _mtg_vis_gray_lut()


# --------------------------------------------------------------------------
# Geometry


def smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def solar_terms(when: dt.datetime) -> tuple[float, float, float]:
    """(declination in radians, equation of time in minutes, UTC hours)."""
    when = when.astimezone(UTC)
    hours = when.hour + when.minute / 60.0 + when.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (when.timetuple().tm_yday - 1 + (hours - 12.0) / 24.0)
    declination = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    equation_of_time = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    return declination, equation_of_time, hours


def cos_solar_zenith(
    longitude: np.ndarray, latitude: np.ndarray, when: dt.datetime, minutes: np.ndarray | float = 0.0,
) -> np.ndarray:
    """NOAA low-precision solar position (≈0.1°), used for day/night blending.

    ``minutes`` optionally shifts the time per pixel, e.g. the scan offset.
    """
    declination, equation_of_time, hours = solar_terms(when)
    true_solar_minutes = (hours * 60.0 + equation_of_time + np.asarray(minutes, dtype=np.float64)
                          + 4.0 * np.asarray(longitude, dtype=np.float64))
    hour_angle = np.deg2rad(true_solar_minutes / 4.0 - 180.0)
    lat = np.deg2rad(np.asarray(latitude, dtype=np.float64))
    value = np.sin(lat) * math.sin(declination) + np.cos(lat) * math.cos(declination) * np.cos(hour_angle)
    return value.astype(np.float32)


# Full-disk scan duration (minutes) and direction. FCI and SEVIRI scan from
# south to north, ABI and AHI from north to south. Durations are approximate;
# the error they leave is far below the 10-minute frame step.
SCAN_TIMING = {
    "MTG": (8.3, True), "FES": (12.0, True), "IODC": (12.0, True),
    "G18": (9.5, False), "G19": (9.5, False), "H09": (9.5, False),
}


def scan_offset_minutes(longitude: np.ndarray, latitude: np.ndarray, satellite_lon: float, name: str) -> np.ndarray:
    """Minutes after the nominal scan start at which each pixel was imaged.

    Uses the north–south scan angle seen from the satellite (±8.9° spans the
    disk); off-disk pixels clamp to the scan's ends.
    """
    duration, south_to_north = SCAN_TIMING[name]
    radius, orbit = 6378.137, 42164.0
    lat = np.deg2rad(np.asarray(latitude, dtype=np.float64))
    delta_lon = np.deg2rad(np.asarray(longitude, dtype=np.float64) - satellite_lon)
    elevation = np.degrees(np.arctan2(radius * np.sin(lat), orbit - radius * np.cos(lat) * np.cos(delta_lon)))
    fraction = (elevation + 8.9) / 17.8 if south_to_north else (8.9 - elevation) / 17.8
    return np.clip(fraction, 0.0, 1.0) * duration


def view_cosine(longitude: np.ndarray, latitude: np.ndarray, satellite_lon: float) -> np.ndarray:
    """Cosine of the geostationary viewing zenith; negative beyond the horizon."""
    earth_radius = 6_378_137.0
    r_sat = earth_radius + 35_786_023.0
    lat = np.deg2rad(latitude)
    delta_lon = np.deg2rad(((np.asarray(longitude) - satellite_lon + 180.0) % 360.0) - 180.0)
    cos_central = np.cos(lat) * np.cos(delta_lon)
    distance = np.sqrt(r_sat**2 + earth_radius**2 - 2.0 * r_sat * earth_radius * cos_central)
    return ((r_sat * cos_central - earth_radius) / distance).astype(np.float32)


def polar_limit(longitude: np.ndarray) -> np.ndarray:
    """Largest |latitude| shown at each longitude."""
    lon = ((np.asarray(longitude, dtype=np.float64) + 180.0) % 360.0) - 180.0
    notch_lon, notch_lat = zip(*POLAR_NOTCH)
    return np.minimum(POLAR_CAP_DEG, np.interp(lon, notch_lon, notch_lat, left=POLAR_CAP_DEG, right=POLAR_CAP_DEG))


def mercator_lon(px: np.ndarray, world_px: int = WORLD_PX) -> np.ndarray:
    return np.asarray(px, dtype=np.float64) / world_px * 360.0 - 180.0


def mercator_lat(py: np.ndarray, world_px: int = WORLD_PX) -> np.ndarray:
    n = math.pi * (1.0 - 2.0 * np.asarray(py, dtype=np.float64) / world_px)
    return np.rad2deg(np.arctan(np.sinh(n)))


def upsample_matrix(size: int, step: int) -> np.ndarray:
    """(size × size/step+1) bilinear weights from node grid to pixel centres."""
    nodes = size // step + 1
    t = (np.arange(size, dtype=np.float64) + 0.5) / step
    k = np.minimum(np.floor(t).astype(int), nodes - 2)
    frac = t - k
    matrix = np.zeros((size, nodes), dtype=np.float32)
    matrix[np.arange(size), k] = 1.0 - frac
    matrix[np.arange(size), k + 1] = frac
    return matrix


# --------------------------------------------------------------------------
# Native layers


@dataclasses.dataclass(frozen=True)
class Geos:
    """Geostationary projection parameters (PROJ ``geos``)."""

    lon_0: float
    h: float
    a: float
    b: float
    sweep_x: bool

    @classmethod
    def from_crs(cls, crs) -> "Geos":
        params = crs.to_dict()
        if params.get("proj") != "geos":
            raise ValueError(f"Unsupported native projection: {params}")
        a = float(params.get("a", 6378137.0))
        if "b" in params:
            b = float(params["b"])
        elif "rf" in params:
            b = a * (1.0 - 1.0 / float(params["rf"]))
        else:
            b = 6356752.31414
        return cls(float(params.get("lon_0", 0.0)), float(params["h"]), a, b, params.get("sweep", "y") == "x")

    def forward(self, longitude: np.ndarray, latitude: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Project to scan-angle metres; NaN where the satellite cannot see.

        Closed form from PROJ's geos.cpp so forked workers never touch pyproj
        (its sqlite teardown crashes after fork on macOS).
        """
        radius_p = self.b / self.a
        radius_g_1 = self.h / self.a
        radius_g = 1.0 + radius_g_1
        lam = np.deg2rad(((np.asarray(longitude, dtype=np.float64) - self.lon_0 + 180.0) % 360.0) - 180.0)
        phi = np.arctan(radius_p * radius_p * np.tan(np.deg2rad(np.asarray(latitude, dtype=np.float64))))
        r = radius_p / np.hypot(radius_p * np.cos(phi), np.sin(phi))
        vx = r * np.cos(lam) * np.cos(phi)
        vy = r * np.sin(lam) * np.cos(phi)
        vz = r * np.sin(phi)
        tmp = radius_g - vx
        hidden = (tmp * vx - vy * vy - vz * vz / (radius_p * radius_p)) < 0.0
        if self.sweep_x:
            x = radius_g_1 * np.arctan(vy / np.hypot(vz, tmp))
            y = radius_g_1 * np.arctan(vz / tmp)
        else:
            x = radius_g_1 * np.arctan(vy / tmp)
            y = radius_g_1 * np.arctan(vz / np.hypot(vy, tmp))
        x = np.where(hidden, np.nan, x * self.a)
        y = np.where(hidden, np.nan, y * self.a)
        return x, y


@dataclasses.dataclass
class NativeLayer:
    """One native uint8-coded image with its affine grid.

    ``geos`` is None for plate carrée grids (x = longitude, y = latitude) or
    the geostationary projection of a full-disk scan in metres. ``x0``/``y0``
    are the outer upper-left corner; ``dx``/``dy`` are positive pixel sizes.
    """

    codes: np.ndarray
    geos: Geos | None
    x0: float
    y0: float
    dx: float
    dy: float
    clipped_box: bool = False  # feather edges when the grid is a clipped box

    def fractional_index(self, longitude: np.ndarray, latitude: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.geos is None:
            x, y = longitude, latitude
        else:
            x, y = self.geos.forward(longitude, latitude)
        col = (np.asarray(x) - self.x0) / self.dx - 0.5
        row = (self.y0 - np.asarray(y)) / self.dy - 0.5
        bad = ~(np.isfinite(col) & np.isfinite(row))
        col = np.where(bad, -1e6, col)
        row = np.where(bad, -1e6, row)
        return row.astype(np.float32), col.astype(np.float32)


@dataclasses.dataclass
class Source:
    name: str
    label: str
    satellite_lon: float
    penalty: float
    ir: NativeLayer | None = None
    vis: NativeLayer | None = None
    vis_is_blue: bool = False
    info: dict = dataclasses.field(default_factory=dict)
    # Nominal scan start actually used (WCS sources may fall back to earlier
    # slots); visible reflectance is normalised by the sun at this time.
    time: dt.datetime | None = None


def bilinear_sample(codes: np.ndarray, row: np.ndarray, col: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Validity-normalised bilinear sample of a uint8 code image.

    Returns (value, valid_fraction); zeros in ``codes`` are treated as missing.
    """
    height, width = codes.shape
    inside = (row > -0.5) & (row < height - 0.5) & (col > -0.5) & (col < width - 0.5)
    r0 = np.clip(np.floor(row).astype(np.int32), 0, height - 2)
    c0 = np.clip(np.floor(col).astype(np.int32), 0, width - 2)
    fr = np.clip(row - r0, 0.0, 1.0).astype(np.float32)
    fc = np.clip(col - c0, 0.0, 1.0).astype(np.float32)
    flat = codes.ravel()
    base = r0.astype(np.int64) * width + c0
    numerator = np.zeros(row.shape, dtype=np.float32)
    denominator = np.zeros(row.shape, dtype=np.float32)
    for offset, weight in (
        (0, (1 - fr) * (1 - fc)),
        (1, (1 - fr) * fc),
        (width, fr * (1 - fc)),
        (width + 1, fr * fc),
    ):
        sample = flat[base + offset].astype(np.float32)
        weight = weight * (sample > 0)
        numerator += weight * sample
        denominator += weight
    denominator *= inside
    value = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-6)
    return value, denominator


# --------------------------------------------------------------------------
# Source acquisition


GOES_SATELLITES = {"G18": ("noaa-goes18", -137.0), "G19": ("noaa-goes19", -75.2)}
HIMAWARI = ("noaa-himawari9", 140.7)


def native_objects(when: dt.datetime) -> list[dict]:
    doy = when.timetuple().tm_yday
    stamp = f"{when.year}{doy:03}{when.hour:02}{when.minute:02}"
    objects: list[dict] = []
    for sat, (bucket, _) in GOES_SATELLITES.items():
        rows = support.list_bucket(bucket, f"ABI-L1b-RadF/{when.year}/{doy:03}/{when.hour:02}/")
        for channel in ("C13", "C01"):
            matches = sorted((k, s) for k, s in rows if f"M6{channel}_{sat}_s{stamp}" in k)
            if not matches:
                print(f"No {sat} {channel} full-disk scan for {stamp}", flush=True)
                continue
            key, size = matches[-1]
            objects.append({"satellite": sat, "channel": channel, "bucket": bucket, "key": key, "size": size})
    bucket = HIMAWARI[0]
    rows = support.list_bucket(bucket, f"AHI-L1b-FLDK/{when:%Y/%m/%d/%H%M}/")
    for band, resolution in (("B13", "R20"), ("B01", "R10")):
        matches = sorted((k, s) for k, s in rows if f"_{band}_FLDK_{resolution}_" in k)
        if len(matches) != 10:
            print(f"Himawari-9 {band} has {len(matches)}/10 segments at {when:%H:%M}", flush=True)
        objects.extend(
            {"satellite": "H09", "channel": band, "bucket": bucket, "key": k, "size": s} for k, s in matches
        )
    return objects


def missing_inputs(when: dt.datetime, include_mtg: bool = True) -> list[str]:
    """Primary inputs for ``when`` not yet published (GOES, Himawari, MTG).

    Meteosat 0° and IODC are not checked: they scan every 15 minutes and the
    previous slot is an acceptable fallback. MTG may be excluded when finding
    the newest bootstrap slot because its WCS reader falls back to recent scans.
    """
    missing: list[str] = []
    doy = when.timetuple().tm_yday
    stamp = f"{when.year}{doy:03}{when.hour:02}{when.minute:02}"
    for sat, (bucket, _) in GOES_SATELLITES.items():
        keys = [k for k, _ in support.list_bucket(bucket, f"ABI-L1b-RadF/{when.year}/{doy:03}/{when.hour:02}/")]
        missing += [f"{sat} {ch}" for ch in ("C13", "C01") if not any(f"M6{ch}_{sat}_s{stamp}" in k for k in keys)]
    keys = [k for k, _ in support.list_bucket(HIMAWARI[0], f"AHI-L1b-FLDK/{when:%Y/%m/%d/%H%M}/")]
    for band, resolution in (("B13", "R20"), ("B01", "R10")):
        if sum(f"_{band}_FLDK_{resolution}_" in k for k in keys) < 10:
            missing.append(f"H09 {band}")
    if include_mtg:
        # FCI scans south to north, so the northern edge of the box arrives last.
        ir_coverage, vis_coverage, *_ = WCS_SOURCES["MTG"]
        for coverage in (ir_coverage, vis_coverage):
            try:
                wcs_get(coverage, when, (69.9, 70.0), (0.0, 0.1), deadline=time.monotonic() + 30)
            except WcsMissing:
                missing.append(f"MTG {coverage}")
            except Exception:  # EUMETView trouble is handled by the download's own fallbacks
                pass
    return missing


def download_native(
    when: dt.datetime, only: set[tuple[str, str]] | None = None,
) -> tuple[dict[tuple[str, str], list[dict]], set[tuple[str, str]]]:
    """Download the listed native scans for ``when``, optionally only some channels.

    Returns the downloads grouped by (satellite, channel) and every channel listed.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    objects = [obj for obj in native_objects(when)
               if only is None or (obj["satellite"], obj["channel"]) in only]
    listed = {(obj["satellite"], obj["channel"]) for obj in objects}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(support.download_object, obj): obj for obj in objects}
        for future in concurrent.futures.as_completed(futures):
            obj = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # a missing channel only removes that layer
                print(f"Download failed for {obj['key']}: {exc}", flush=True)
                continue
            grouped.setdefault((obj["satellite"], obj["channel"]), []).append(result)
    for items in grouped.values():
        items.sort(key=lambda item: item["key"])
    return grouped, listed


NATIVE_CHANNELS = {
    ("G18", "C13"): ("abi_l1b", "ir"), ("G18", "C01"): ("abi_l1b", "vis"),
    ("G19", "C13"): ("abi_l1b", "ir"), ("G19", "C01"): ("abi_l1b", "vis"),
    ("H09", "B13"): ("ahi_hsd", "ir"), ("H09", "B01"): ("ahi_hsd", "vis"),
}


def load_native_layers(when: dt.datetime) -> dict[tuple[str, str], tuple[NativeLayer, dict]]:
    """Download and decode the GOES and Himawari channels for ``when``.

    A listed channel that fails to download or decode is fetched once more: a
    transient endpoint error, or another run pruning the shared cache, would
    otherwise publish the frame without that satellite.
    """
    layers: dict[tuple[str, str], tuple[NativeLayer, dict]] = {}
    wanted: set[tuple[str, str]] | None = None
    for attempt in range(2):
        grouped, listed = download_native(when, wanted)
        for key in sorted(listed):
            files = [item["path"] for item in grouped.get(key, [])]
            if not files:
                continue
            reader, kind = NATIVE_CHANNELS[key]
            try:
                layers[key] = load_native_layer(files, reader, key[1], kind)
            except Exception as exc:
                print(f"{key[0]} {key[1]} load failed: {exc}", flush=True)
        wanted = listed - layers.keys()
        if not wanted:
            break
        if attempt == 0:
            print(f"Retrying {', '.join(f'{sat} {channel}' for sat, channel in sorted(wanted))}", flush=True)
    return layers


def load_native_layer(paths: list[str], reader: str, dataset: str, kind: str) -> tuple[NativeLayer, dict]:
    from satpy import Scene

    started = perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        scene = Scene(filenames=paths, reader=reader)
        scene.load([dataset])
        data = scene[dataset]
        values = np.asarray(data.values, dtype=np.float32)
    area = data.attrs["area"]
    if kind == "ir":
        codes = ir_code(values)
    else:
        # Satpy reports reflectance factor in percent.
        codes = reflectance_code(values / 100.0)
    x0, _, _, y1 = area.area_extent
    layer = NativeLayer(
        codes=codes,
        geos=Geos.from_crs(area.crs),
        x0=float(x0),
        y0=float(y1),
        dx=float(area.pixel_size_x),
        dy=float(area.pixel_size_y),
    )
    info = {
        "dataset": dataset,
        "native_shape": list(values.shape),
        "files": len(paths),
        "start_time": str(data.attrs.get("start_time", "")),
        "valid_fraction": round(float((codes > 0).mean()), 4),
        "load_seconds": round(perf_counter() - started, 2),
    }
    del scene, data, values
    gc.collect()
    return layer, info


class WcsMissing(Exception):
    """The requested coverage time does not exist (yet)."""


class WcsIncomplete(WcsMissing):
    """A coverage has failed chunks and must not enter the satellite mosaic."""


def wcs_get(
    coverage: str,
    when: dt.datetime,
    lat: tuple[float, float],
    lon: tuple[float, float],
    scale: float | None = None,
    deadline: float | None = None,
) -> bytes:
    params = [
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageId", coverage),
        ("subset", f"Lat({lat[0]:.6f},{lat[1]:.6f})"),
        ("subset", f"Long({lon[0]:.6f},{lon[1]:.6f})"),
        ("subset", f'time("{when:%Y-%m-%dT%H:%M}:00.000Z")'),
        ("format", "image/tiff"), ("geotiff:compression", "Deflate"),
    ]
    if scale is not None:
        params.append(("scaleFactor", f"{scale:.6f}"))
    url = f"{WCS_URL}?{urllib.parse.urlencode(params)}"
    error: Exception | None = None
    for attempt in range(8):
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            request = urllib.request.Request(url, headers={"User-Agent": WCS_USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
            if body[:4] not in (b"II*\x00", b"MM\x00*"):
                raise RuntimeError(f"WCS returned non-TIFF payload for {coverage}")
            return body
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:2000]
            if exc.code in (404, 500) and b"ExceptionReport" in detail:
                raise WcsMissing(f"{coverage} {when:%H:%M}: HTTP {exc.code}") from exc
            error = exc
        except Exception as exc:  # rate limiting and transient network errors
            error = exc
        time.sleep(min(30.0, 2.0 * 1.6**attempt))
    raise RuntimeError(f"WCS request failed for {coverage}: {error}")


def decode_geotiff(body: bytes, band: int = 0) -> tuple[np.ndarray, float, float, float, float]:
    """Return (gray, lon0, lat0, dlon, dlat) of an EPSG:4326 GeoTIFF."""
    with Image.open(io.BytesIO(body)) as image:
        transform = image.tag_v2.get(34264)
        array = np.asarray(image)
    if array.ndim == 3:
        array = array[..., band]
    if transform is None:
        raise RuntimeError("WCS GeoTIFF lacks a model transformation")
    dlon, lon0, dlat, lat0 = transform[0], transform[3], -transform[5], transform[7]
    return np.ascontiguousarray(array, dtype=np.uint8), lon0, lat0, dlon, dlat


WCS_SOURCES = {
    # name: (ir coverage, vis coverage, cadence minutes, lat box, lon box, chunk degrees, vis scale)
    # MTG VIS HRFI is 0.5 km (0.00712°); scaling it to the IR's 0.01° grid
    # halves the download without losing detail at z5.
    "MTG": ("mtg_fd__ir105_hrfi", "mtg_fd__vis06_hrfi", 10, (-70.0, 70.0), (-70.0, 70.0), 35.0, 0.7125),
    "IODC": ("msg_iodc__ir108", "msg_iodc__vis006", 15, (-76.98, 76.98), (-35.5, 118.5), None, None),
    "FES": ("msg_fes__ir108", "msg_fes__vis006", 15, (-76.98, 76.98), (-76.98, 76.98), None, None),
}
# Incomplete MTG coverages are rejected: filling individual chunks from MSG
# creates rectangular seams because the products have different radiometry.
WCS_DEADLINE_SECONDS = float(os.environ.get("SATELLITE_WCS_DEADLINE_SECONDS", "330"))
WCS_IR_LUTS = {"MTG": anchor_lut(MTG_IR_ANCHORS), "IODC": anchor_lut(IODC_IR_ANCHORS), "FES": anchor_lut(FES_IR_ANCHORS)}


def fetch_wcs_coverage(
    name: str, coverage: str, when: dt.datetime, lut: np.ndarray, scale: float | None, deadline: float
) -> NativeLayer:
    _, _, _, lat_box, lon_box, chunk, _ = WCS_SOURCES[name]
    if chunk is None:
        gray, lon0, lat0, dlon, dlat = decode_geotiff(wcs_get(coverage, when, lat_box, lon_box, scale, deadline))
        return NativeLayer(lut[gray], None, lon0, lat0, dlon, dlat, clipped_box=True)

    lat_edges = np.arange(lat_box[0], lat_box[1] + 1e-6, chunk)
    lon_edges = np.arange(lon_box[0], lon_box[1] + 1e-6, chunk)
    requests = [
        (lat_edges[i], lat_edges[i + 1], lon_edges[j], lon_edges[j + 1])
        for i in range(len(lat_edges) - 1) for j in range(len(lon_edges) - 1)
    ]
    # Check the centre chunk first so missing times fail quickly.
    requests.sort(key=lambda box: abs(box[0] + box[1]) + abs(box[2] + box[3]))
    first = decode_geotiff(wcs_get(coverage, when, requests[0][:2], requests[0][2:], scale, deadline))
    dlon, dlat = first[3], first[4]
    width = int(round((lon_box[1] - lon_box[0]) / dlon))
    height = int(round((lat_box[1] - lat_box[0]) / dlat))
    canvas = np.zeros((height, width), dtype=np.uint8)

    def place(result: tuple[np.ndarray, float, float, float, float]) -> None:
        gray, lon0, lat0, _, _ = result
        col = int(round((lon0 - lon_box[0]) / dlon))
        row = int(round((lat_box[1] - lat0) / dlat))
        h = min(gray.shape[0], height - row)
        w = min(gray.shape[1], width - col)
        canvas[row:row + h, col:col + w] = lut[gray[:h, :w]]

    place(first)
    failed_boxes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WCS_WORKERS) as pool:
        futures = {pool.submit(wcs_get, coverage, when, box[:2], box[2:], scale, deadline): box
                   for box in requests[1:]}
        for future in concurrent.futures.as_completed(futures):
            try:
                place(decode_geotiff(future.result()))
            except Exception as exc:
                box = futures[future]
                failed_boxes.append(box)
                print(f"{coverage} chunk {box} failed: {exc}", flush=True)
    if failed_boxes:
        raise WcsIncomplete(
            f"{coverage} {when:%H:%M}: rejected incomplete coverage; "
            f"{len(failed_boxes)}/{len(requests)} chunks failed: {sorted(failed_boxes)}"
        )
    return NativeLayer(canvas, None, lon_box[0], lat_box[1], dlon, dlat, clipped_box=True)


def fetch_wcs_source(
    name: str, when: dt.datetime, deadline: float
) -> tuple[NativeLayer | None, NativeLayer | None, dict]:
    ir_coverage, vis_coverage, cadence, *_, vis_scale = WCS_SOURCES[name]
    vis_lut = MTG_VIS_GRAY_LUT if name == "MTG" else VIS_GRAY_LUT
    started = perf_counter()
    base = when - dt.timedelta(minutes=when.minute % cadence)
    # IR and VIS requests are fetched concurrently.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        for fallback in range(0, 31, cadence):
            attempt = base - dt.timedelta(minutes=fallback)
            vis_future = pool.submit(
                fetch_wcs_coverage, name, vis_coverage, attempt, vis_lut, vis_scale, deadline
            )
            try:
                ir = fetch_wcs_coverage(name, ir_coverage, attempt, WCS_IR_LUTS[name], None, deadline)
            except WcsMissing as exc:
                print(f"{name} WCS IR unavailable at {attempt:%H:%M}: {exc}", flush=True)
                vis_future.cancel()
                concurrent.futures.wait([vis_future])
                continue
            except Exception as exc:
                print(f"{name} WCS IR failed: {exc}", flush=True)
                concurrent.futures.wait([vis_future])
                break
            try:
                vis = vis_future.result()
            except WcsMissing as exc:
                # Retry both channels at the same earlier time. Combining a
                # complete IR scan with partial VIS still produces rectangles.
                print(f"{name} WCS VIS unavailable at {attempt:%H:%M}: {exc}", flush=True)
                continue
            except Exception as exc:
                print(f"{name} WCS VIS unavailable at {attempt:%H:%M}: {exc}", flush=True)
                vis = None
            return ir, vis, {
                "available": True,
                "timestamp": attempt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fallback_minutes": int((when - attempt).total_seconds() // 60),
                "vis_available": vis is not None,
                "fetch_seconds": round(perf_counter() - started, 1),
            }
    finally:
        pool.shutdown(wait=True)
    return None, None, {"available": False, "fetch_seconds": round(perf_counter() - started, 1)}


def acquire_sources(when: dt.datetime, include_wcs: bool = True) -> list[Source]:
    """Download and decode every source for ``when`` into native code layers."""
    wcs_results: dict[str, tuple] = {}
    wcs_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    wcs_future = None
    if include_wcs:
        # EUMETView throttles aggressively; fetch its products serially in the
        # background while NOAA inputs download and decode.
        deadline = time.monotonic() + WCS_DEADLINE_SECONDS

        def fetch_all() -> None:
            # Meteosat 0° first: it is small and backs up every MTG chunk.
            for name in ("FES", "IODC", "MTG"):
                wcs_results[name] = fetch_wcs_source(name, when, deadline)
                print(f"{name}: {wcs_results[name][2]}", flush=True)
        wcs_future = wcs_pool.submit(fetch_all)

    layers = load_native_layers(when)
    sources = [
        Source(sat, {"G18": "GOES-18", "G19": "GOES-19"}[sat], lon, 0.0, vis_is_blue=True, time=when)
        for sat, (_, lon) in GOES_SATELLITES.items()
    ]
    sources.append(Source("H09", "Himawari-9", HIMAWARI[1], 0.0, vis_is_blue=True, time=when))
    by_name = {source.name: source for source in sources}
    for (sat, channel), (layer, info) in layers.items():
        kind = NATIVE_CHANNELS[(sat, channel)][1]
        setattr(by_name[sat], kind, layer)
        by_name[sat].info[kind] = info

    if wcs_future is not None:
        wcs_future.result()
    wcs_pool.shutdown()
    labels = {"MTG": ("Meteosat MTG", 0.0, 0.0), "IODC": ("Meteosat IODC", 45.5, -0.15), "FES": ("Meteosat 0°", 0.0, -0.1)}
    for name, (label, lon, penalty) in labels.items():
        if name not in wcs_results:
            continue
        ir, vis, info = wcs_results[name]
        scan_time = (dt.datetime.strptime(info["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                     if "timestamp" in info else when)
        sources.append(Source(name, label, lon, penalty, ir=ir, vis=vis, info=info, time=scan_time))
    return sources


# --------------------------------------------------------------------------
# Mosaic


_SOURCES: list[Source] = []
_WHEN: dt.datetime | None = None
_UPSAMPLE = upsample_matrix(BLOCK_SIZE, NODE_STEP)


def _upsample(nodes: np.ndarray) -> np.ndarray:
    return _UPSAMPLE @ nodes.astype(np.float32) @ _UPSAMPLE.T


def _box_fade(layer: NativeLayer, row: np.ndarray, col: np.ndarray) -> np.ndarray:
    height, width = layer.codes.shape
    distance = np.minimum(np.minimum(row, height - 1 - row), np.minimum(col, width - 1 - col))
    return np.clip(distance / BOX_EDGE_FADE_PX, 0.0, 1.0).astype(np.float32)


def render_block(block: tuple[int, int], world_px: int = WORLD_PX) -> dict | None:
    """Render one BLOCK_SIZE² region at the pack's maximum zoom."""
    bx, by = block
    pixels = np.arange(BLOCK_SIZE, dtype=np.float64) + 0.5
    pixel_lat = mercator_lat(by * BLOCK_SIZE + pixels, world_px)
    pixel_limit = polar_limit(mercator_lon(bx * BLOCK_SIZE + pixels, world_px))
    if np.abs(pixel_lat).min() > pixel_limit.max():
        return None
    inside_cap = np.abs(pixel_lat)[:, None] <= pixel_limit[None, :]

    nodes = np.arange(0, BLOCK_SIZE + 1, NODE_STEP, dtype=np.float64)
    node_lon = mercator_lon(bx * BLOCK_SIZE + nodes, world_px)
    node_lat = mercator_lat(by * BLOCK_SIZE + nodes, world_px)
    lon_grid, lat_grid = np.meshgrid(node_lon, node_lat)

    candidates = []
    for source in _SOURCES:
        if source.ir is None and source.vis is None:
            continue
        cos_nodes = view_cosine(lon_grid, lat_grid, source.satellite_lon)
        if cos_nodes.max() <= LIMB_FADE[0]:
            continue
        candidates.append((source, cos_nodes))
    if not candidates:
        return None

    shape = (BLOCK_SIZE, BLOCK_SIZE)
    scores = []
    for source, cos_nodes in candidates:
        cos_pixels = _upsample(cos_nodes)
        scores.append((source, cos_pixels + source.penalty, smoothstep(*LIMB_FADE, cos_pixels)))
    best = np.max(np.stack([score for _, score, _ in scores]), axis=0)

    sums = {"ir": [np.zeros(shape, np.float32), np.zeros(shape, np.float32)],
            "vis": [np.zeros(shape, np.float32), np.zeros(shape, np.float32)]}
    for source, score, fade in scores:
        weight = np.exp((score - best) / SOFTMAX_TAU) * fade
        active = weight > 1e-6
        if not active.any():
            continue
        for kind in ("ir", "vis"):
            layer: NativeLayer | None = getattr(source, kind)
            if layer is None:
                continue
            # Sample only pixels this source can affect. Visible reflectance is
            # normalised by the sun when this source imaged each pixel (its
            # actual slot plus scan offset), not at the frame time: a fallback
            # scan or a late-scanned row would otherwise brighten as the frame
            # sun rises.
            if kind == "vis":
                source_sun = _upsample(cos_solar_zenith(
                    lon_grid, lat_grid, source.time or _WHEN,
                    scan_offset_minutes(lon_grid, lat_grid, source.satellite_lon, source.name),
                ))
                mask = active & (source_sun > NIGHT_COS_SZA)
            else:
                mask = active
            if mask.all():
                pick = slice(None)
            elif mask.any():
                pick = np.flatnonzero(mask)
            else:
                continue
            row_nodes, col_nodes = layer.fractional_index(lon_grid, lat_grid)
            row, col = _upsample(row_nodes).reshape(-1)[pick], _upsample(col_nodes).reshape(-1)[pick]
            value, valid = bilinear_sample(layer.codes, row, col)
            if kind == "vis":
                reflectance = reflectance_from_code(value)
                if source.vis_is_blue:
                    reflectance = blue_to_red_reflectance(reflectance)
                sun = source_sun.reshape(-1)[pick]
                normalised = remove_low_sun_haze(reflectance / np.maximum(sun, VIS_NORMALISATION_FLOOR), sun)
                # Blend in code space, unrounded, so overlaps stay smooth.
                value = np.where(valid > 0, 1.0 + 254.0 * np.sqrt(np.clip(normalised, 0.0, 1.0)), 0.0)
            layer_weight = weight.reshape(-1)[pick] * valid
            if layer.clipped_box:
                layer_weight *= _box_fade(layer, row, col)
            sums[kind][0].reshape(-1)[pick] += layer_weight * value
            sums[kind][1].reshape(-1)[pick] += layer_weight

    ir_num, ir_den = sums["ir"]
    ir = np.where(ir_den > 1e-4, np.clip(np.rint(ir_num / np.maximum(ir_den, 1e-9)), 1, 255), 0).astype(np.uint8)

    vis_num, vis_den = sums["vis"]
    has_vis = vis_den > 1e-4
    vis = np.where(has_vis, np.clip(np.rint(vis_num / np.maximum(vis_den, 1e-9)), 1, 255), 0).astype(np.uint8)
    ir[~inside_cap] = 0
    vis[~inside_cap] = 0
    if not ir.any() and not vis.any():
        return None
    return {"block": block, "ir": ir, "vis": vis}


# Correct low-sun haze before encoding visible reflectance. The haze is
# subtracted, not subtracted and re-stretched: stretching by 1/(1 - base)
# brightened every cloud by up to 22% at low sun, and that gain changed from
# one frame to the next.
HAZE_COS = np.array([0, .02, .06, .10, .14, .18, .22, .26, .30, .34, .40, .50, .60, .75])
HAZE_BASE = np.array([0, .05, .085, .12, .15, .17, .18, .17, .16, .15, .13, .10, .08, 0])


def remove_low_sun_haze(normalised: np.ndarray, cos_sun: np.ndarray) -> np.ndarray:
    base = np.interp(cos_sun, HAZE_COS, HAZE_BASE)
    return np.maximum(normalised - base, 0)


def downsample_codes(codes: np.ndarray) -> np.ndarray:
    """2× reduce, averaging non-zero codes; a cell needs ≥2 valid children."""
    h, w = codes.shape
    blocks = codes.reshape(h // 2, 2, w // 2, 2).astype(np.uint16)
    valid = (blocks > 0).sum(axis=(1, 3))
    total = blocks.sum(axis=(1, 3))
    mean = np.divide(total, valid, out=np.zeros(valid.shape, dtype=np.float64), where=valid > 0)
    return np.where(valid >= 2, np.clip(np.rint(mean), 1, 255), 0).astype(np.uint8)


# Workers parallelise tile encoding, so each encoder uses one thread.
AVIF_SPEED = 6


def encode_tile(ir: np.ndarray, vis: np.ndarray, quality: int) -> bytes:
    if not ir.any() and not vis.any():
        return b""
    image = Image.fromarray(np.vstack([ir, vis]), "L")
    buffer = io.BytesIO()
    image.save(buffer, "AVIF", quality=quality, speed=AVIF_SPEED, max_threads=1)
    return buffer.getvalue()


def _tiles_below(z: int) -> int:
    """Tiles in zooms MIN_ZOOM..z-1."""
    return ((1 << (2 * z)) - (1 << (2 * MIN_ZOOM))) // 3


def tile_index(z: int, x: int, y: int) -> int:
    return _tiles_below(z) + y * (1 << z) + x


def index_entry_count() -> int:
    return _tiles_below(MAX_ZOOM + 1)


def _render_and_encode(args: tuple[tuple[int, int], int]) -> tuple[tuple[int, int], dict[tuple[int, int], bytes], np.ndarray | None, np.ndarray | None]:
    block, quality = args
    result = render_block(block)
    if result is None:
        return block, {}, None, None
    tiles_per_block = BLOCK_SIZE // TILE_SIZE
    encoded = {}
    for ty in range(tiles_per_block):
        for tx in range(tiles_per_block):
            window = (slice(ty * TILE_SIZE, (ty + 1) * TILE_SIZE), slice(tx * TILE_SIZE, (tx + 1) * TILE_SIZE))
            data = encode_tile(result["ir"][window], result["vis"][window], quality)
            if data:
                encoded[(block[0] * tiles_per_block + tx, block[1] * tiles_per_block + ty)] = data
    return block, encoded, downsample_codes(result["ir"]), downsample_codes(result["vis"])


def _encode_window(args: tuple[np.ndarray, np.ndarray, int]) -> bytes:
    return encode_tile(*args)


def build_frame_pack(
    when: dt.datetime,
    sources: list[Source],
    output: Path,
    quality: int = 50,
    workers: int = 4,
) -> dict:
    """Render all MIN_ZOOM–MAX_ZOOM tiles for ``when`` and write one pack file."""
    global _SOURCES, _WHEN
    _SOURCES, _WHEN = sources, when
    started = perf_counter()
    per_axis = WORLD_PX // BLOCK_SIZE
    blocks = [(bx, by) for by in range(per_axis) for bx in range(per_axis)]
    tiles: dict[tuple[int, int, int], bytes] = {}
    half = WORLD_PX // 2
    canvas_ir = np.zeros((half, half), dtype=np.uint8)
    canvas_vis = np.zeros((half, half), dtype=np.uint8)
    context = mp.get_context("fork")
    with context.Pool(workers) as pool:
        for block, encoded, ir_half, vis_half in pool.imap_unordered(
            _render_and_encode, [(block, quality) for block in blocks], chunksize=1
        ):
            for (x, y), data in encoded.items():
                tiles[(MAX_ZOOM, x, y)] = data
            if ir_half is not None:
                size = BLOCK_SIZE // 2
                window = (slice(block[1] * size, (block[1] + 1) * size), slice(block[0] * size, (block[0] + 1) * size))
                canvas_ir[window] = ir_half
                canvas_vis[window] = vis_half
        render_seconds = perf_counter() - started

        coverage = float((canvas_ir > 0).mean())
        for z in range(MAX_ZOOM - 1, MIN_ZOOM - 1, -1):
            count = 1 << z
            jobs, addresses = [], []
            for y in range(count):
                for x in range(count):
                    window = (slice(y * TILE_SIZE, (y + 1) * TILE_SIZE), slice(x * TILE_SIZE, (x + 1) * TILE_SIZE))
                    jobs.append((canvas_ir[window].copy(), canvas_vis[window].copy(), quality))
                    addresses.append((z, x, y))
            for address, data in zip(addresses, pool.map(_encode_window, jobs, chunksize=4)):
                if data:
                    tiles[address] = data
            if z > MIN_ZOOM:
                canvas_ir, canvas_vis = downsample_codes(canvas_ir), downsample_codes(canvas_vis)
    _SOURCES, _WHEN = [], None

    size = write_pack(output, tiles)
    return {
        "pack_bytes": size,
        "tiles": len(tiles),
        "coverage_fraction": round(coverage, 5),
        "render_seconds": round(render_seconds, 1),
        "total_seconds": round(perf_counter() - started, 1),
    }


def write_pack(path: Path, tiles: dict[tuple[int, int, int], bytes]) -> int:
    count = index_entry_count()
    offset = PACK_HEADER.size + count * INDEX_ENTRY.size
    index = bytearray(count * INDEX_ENTRY.size)
    order = sorted(tiles, key=lambda address: tile_index(*address))
    for address in order:
        data = tiles[address]
        INDEX_ENTRY.pack_into(index, tile_index(*address) * INDEX_ENTRY.size, offset, len(data))
        offset += len(data)
    if offset >= 1 << 32:
        raise RuntimeError("Tile pack exceeds 4 GiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(PACK_HEADER.pack(PACK_MAGIC, PACK_VERSION, MIN_ZOOM, MAX_ZOOM, count))
        handle.write(index)
        for address in order:
            handle.write(tiles[address])
    temporary.replace(path)
    return offset
