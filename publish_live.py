#!/usr/bin/env python3
"""Build and publish global satellite frames."""

from __future__ import annotations

import datetime as dt
import gc
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import mosaic_v2 as mosaic
from frame_schedule import FRAME_STEP, MAX_FRAME_COUNT, missing_frames, publishable_window
import pipeline_support as support

UTC = dt.timezone.utc
DEFAULT_AVIF_QUALITY = 50
PART_BYTES = 48 * 1024 * 1024  # R2: equal parts ≥ 5 MiB except the last
WORKER_BASE_URL = ""
WORKER_USER_AGENT = "SatellitePipelinePublisher/2.0"
POLL_SECONDS = 30
MAX_PUBLISH_CONFLICTS = 5
DEFAULT_BOOTSTRAP_LOOKBACK_MINUTES = 180
PRODUCT = "Global infrared and visible satellite observations"


def latest_available_frame(now: dt.datetime, lookback_minutes: int) -> dt.datetime:
    """Find the newest ten-minute slot whose native GOES and Himawari inputs are ready."""
    newest = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    for offset in range(0, lookback_minutes + 1, 10):
        candidate = newest - dt.timedelta(minutes=offset)
        missing = mosaic.missing_inputs(candidate, include_mtg=False)
        if not missing:
            print(f"Newest ready source slot is {candidate.isoformat()}", flush=True)
            return candidate
        print(f"Source slot {candidate:%H:%M} is not ready ({', '.join(missing)}); checking the prior slot", flush=True)
    raise RuntimeError(
        f"No complete GOES/Himawari source slot found in the last {lookback_minutes} minutes"
    )


class WorkerHTTPError(RuntimeError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def worker_request(token: str, method: str, path: str, body: bytes | None = None,
                   content_type: str = "application/json", timeout: int = 90, attempts: int = 4) -> dict | None:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": WORKER_USER_AGENT}
    if body is not None:
        headers["Content-Type"] = content_type
    for attempt in range(attempts):
        request = urllib.request.Request(f"{WORKER_BASE_URL}{path}", data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 404 and method == "GET":
                return None
            if error.code < 500 or attempt == attempts - 1:
                raise WorkerHTTPError(f"{method} {path} failed with HTTP {error.code}: {detail}", error.code) from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts - 1:
                raise RuntimeError(f"{method} {path} failed: {error}") from error
        time.sleep(2 ** attempt * 3)
    return None


def upload_pack(token: str, frame_id: str, revision: str, path: Path) -> int:
    base = f"/v2/publish/packs/{frame_id}/{revision}"
    upload_id = worker_request(token, "POST", f"{base}/uploads")["upload_id"]
    size = path.stat().st_size
    parts = []
    try:
        with path.open("rb") as handle:
            number = 1
            while chunk := handle.read(PART_BYTES):
                result = worker_request(
                    token, "PUT", f"{base}/parts/{number}?upload_id={upload_id}", chunk,
                    "application/octet-stream", timeout=300,
                )
                parts.append({"part_number": result["part_number"], "etag": result["etag"]})
                number += 1
        body = json.dumps({"parts": parts, "bytes": size}).encode()
        worker_request(token, "POST", f"{base}/complete?upload_id={upload_id}", body)
    except Exception:
        try:
            worker_request(token, "POST", f"{base}/abort?upload_id={upload_id}", attempts=1)
        except Exception as abort_error:  # the original failure matters more
            print(f"Abort of {frame_id} upload failed: {abort_error}", flush=True)
        raise
    return size


def source_summary(sources: list[mosaic.Source], stats: dict) -> dict:
    availability = {}
    for source in sources:
        entry = {"infrared": source.ir is not None, "visible": source.vis is not None}
        for key in ("timestamp", "fallback_minutes"):
            if key in source.info:
                entry[key] = source.info[key]
        availability[source.label] = entry
    return {
        "channels": ["infrared", "visible"],
        "coverage_fraction": round(float(stats["coverage_fraction"]), 5),
        "sources": [label for label, entry in availability.items() if entry["infrared"]],
        "source_availability": availability,
        "tiles": stats["tiles"],
    }


def build_frame(token: str, when: dt.datetime, revision: str, output_root: Path,
                quality: int, workers: int) -> tuple[dict, int]:
    frame_started = time.perf_counter()
    frame_id = when.strftime("%Y%m%dT%H%MZ")
    sources = mosaic.acquire_sources(when)
    downloaded = time.perf_counter()
    output = output_root / f"{frame_id}.pack"
    stats = mosaic.build_frame_pack(when, sources, output, quality, workers)
    rendered = time.perf_counter()
    summary = source_summary(sources, stats)
    del sources
    gc.collect()
    size = upload_pack(token, frame_id, revision, output)
    output.unlink(missing_ok=True)
    finished = time.perf_counter()
    summary["timing_seconds"] = {
        "download": round(downloaded - frame_started, 1),
        "render": round(rendered - downloaded, 1),
        "upload": round(finished - rendered, 1),
        "total": round(finished - frame_started, 1),
    }
    summary["uploaded_at"] = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Processed {frame_id}: coverage={summary['coverage_fraction']:.1%}, "
          f"tiles={stats['tiles']}, pack={stats['pack_bytes'] / 1e6:.1f} MB, "
          f"seconds={summary['timing_seconds']}", flush=True)
    support.cleanup_input_cache()
    return {
        "id": frame_id,
        "valid_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pack": revision,
        "source_summary": summary,
    }, size


def read_published_frames(token: str) -> dict[dt.datetime, dict] | None:
    """The published timeline keyed by valid time, or ``None`` if it is missing or on another grid."""
    manifest = worker_request(token, "GET", "/v2/publish/manifest")
    frames = (manifest or {}).get("frames", [])
    if not frames or (manifest.get("minimum_zoom_level"), manifest.get("maximum_zoom_level")) != (mosaic.MIN_ZOOM, mosaic.MAX_ZOOM):
        return None
    return {support.parse_utc(frame["valid_time"]): frame for frame in frames}


def publish_manifest(token: str, frames: list[dict], allow_rewind: bool = False) -> None:
    manifest = {
        "schema_version": 2,
        "product": PRODUCT,
        "crs": "EPSG:3857",
        "minimum_zoom_level": mosaic.MIN_ZOOM,
        "maximum_zoom_level": mosaic.MAX_ZOOM,
        "tile_size": mosaic.TILE_SIZE,
        "encoding": mosaic.ENCODING_DESCRIPTOR,
        "frames": [{key: frame[key] for key in ("id", "valid_time", "pack", "source_summary")} for frame in frames],
    }
    path = "/v2/publish/manifest" + ("?allow_rewind=1" if allow_rewind else "")
    result = worker_request(token, "PUT", path, json.dumps(manifest, separators=(",", ":")).encode())
    if not result or result.get("published") is not True:
        raise RuntimeError(f"Manifest publication failed: {result}")


def complete_timeline(token: str, target: dt.datetime, deadline: dt.datetime,
                      built: dict[dt.datetime, dict], build) -> list[str]:
    """Publish ``target`` once the frames before it exist, filling only frames whose owner failed.

    Returns the ids of the manifest this run published last (empty if a newer
    run published first).
    """
    published_ids: list[str] = []
    conflicts = 0
    waiting_for: list[dt.datetime] = []
    while True:
        published = read_published_frames(token) or {}
        latest = max(published, default=None)
        if latest is not None and latest >= target:
            return published_ids
        available = {**built, **published}  # keep published revisions so client caches stay valid
        window = publishable_window(set(available), latest)
        if window:
            frames = [available[when] for when in window]
            try:
                publish_manifest(token, frames)
            except WorkerHTTPError as error:
                # Another run published between our read and write.
                conflicts += 1
                if error.code != 409 or conflicts > MAX_PUBLISH_CONFLICTS:
                    raise
                print(f"Manifest rejected, re-reading the timeline: {error}", flush=True)
                time.sleep(2)
                continue
            published_ids = [frame["id"] for frame in frames]
            print(f"Published timeline through {window[-1].isoformat()}", flush=True)
            continue
        if latest is None:
            missing = missing_frames(target, set(available))
        else:
            # Preserve frame continuity after bootstrap. If a newer run finishes
            # first, wait for the intervening frame instead of advancing past it.
            steps = max(0, (target - latest) // dt.timedelta(minutes=10))
            missing = [
                latest + dt.timedelta(minutes=10 * index)
                for index in range(1, steps + 1)
                if latest + dt.timedelta(minutes=10 * index) not in available
            ]
        if not missing:
            # A concurrent publisher has the needed pack(s); poll for its manifest.
            time.sleep(POLL_SECONDS)
            continue
        if dt.datetime.now(UTC) >= deadline:
            print(f"Owner of {missing[0].isoformat()} did not publish it; rebuilding", flush=True)
            build(missing[0])
            continue
        if missing != waiting_for:
            waiting_for = missing
            print(f"Waiting until {deadline.isoformat()} for runs building "
                  f"{', '.join(when.strftime('%H:%M') for when in missing)}", flush=True)
        time.sleep(POLL_SECONDS)


def wait_for_inputs(when: dt.datetime, until: dt.datetime) -> None:
    """Poll until the primary inputs for ``when`` are published or ``until`` passes."""
    started = time.perf_counter()
    while True:
        try:
            missing = mosaic.missing_inputs(when)
        except Exception as exc:  # listing trouble: wait the full time, as before polling
            missing = [f"input check ({exc})"]
        if not missing:
            print(f"Inputs for {when:%H:%M} ready after {time.perf_counter() - started:.0f}s", flush=True)
            return
        if dt.datetime.now(UTC) >= until:
            print(f"Building {when:%H:%M} without waiting longer for {', '.join(missing)}", flush=True)
            return
        time.sleep(POLL_SECONDS)


def main() -> None:
    global WORKER_BASE_URL
    token = os.environ.get("SATELLITE_PUBLISH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SATELLITE_PUBLISH_TOKEN is required")
    WORKER_BASE_URL = os.environ.get("SATELLITE_PUBLISHER_URL", "").rstrip("/")
    if not WORKER_BASE_URL:
        raise RuntimeError("SATELLITE_PUBLISHER_URL is required")

    count = int(os.environ.get("SATELLITE_FRAME_COUNT", "1"))
    bootstrap_lookback = int(os.environ.get(
        "SATELLITE_BOOTSTRAP_LOOKBACK_MINUTES", str(DEFAULT_BOOTSTRAP_LOOKBACK_MINUTES)
    ))
    max_wait = int(os.environ.get("SATELLITE_INPUT_WAIT_MINUTES", "10"))
    owner_timeout = int(os.environ.get("SATELLITE_OWNER_TIMEOUT_MINUTES", "20"))
    quality = int(os.environ.get("SATELLITE_AVIF_QUALITY", str(DEFAULT_AVIF_QUALITY)))
    workers = int(os.environ.get("SATELLITE_RENDER_WORKERS", str(max(1, min(8, os.cpu_count() or 4)))))
    if count < 1 or count > MAX_FRAME_COUNT:
        raise ValueError("SATELLITE_FRAME_COUNT must be between 1 and 6")
    if bootstrap_lookback < 10 or bootstrap_lookback % 10:
        raise ValueError("SATELLITE_BOOTSTRAP_LOOKBACK_MINUTES must be a positive 10-minute multiple")
    if max_wait < 0:
        raise ValueError("SATELLITE_INPUT_WAIT_MINUTES must not be negative")
    if owner_timeout < 10:
        raise ValueError("SATELLITE_OWNER_TIMEOUT_MINUTES must be at least 10")
    if not 1 <= quality <= 100:
        raise ValueError("SATELLITE_AVIF_QUALITY must be between 1 and 100")

    explicit_start = bool(os.environ.get("SATELLITE_START_UTC"))
    restart_from_latest = os.environ.get("SATELLITE_RESTART_FROM_LATEST", "").strip().lower() in {
        "1", "true", "yes",
    }
    now = dt.datetime.now(UTC)
    published = read_published_frames(token)
    if explicit_start:
        first = support.parse_utc(os.environ["SATELLITE_START_UTC"])
        frame_times = [first + FRAME_STEP * index for index in range(count)]
        bootstrap = False
    else:
        bootstrap = published is None or restart_from_latest
        if bootstrap:
            if restart_from_latest:
                print("Manual restart: selecting the newest available source frame", flush=True)
            else:
                print("No compatible v2 timeline published; selecting the newest available source frame", flush=True)
            latest = latest_available_frame(now, bootstrap_lookback)
            previous_latest = max(published, default=None) if published else None
            if previous_latest is not None and latest <= previous_latest:
                print(
                    f"No source frame newer than the published {previous_latest.isoformat()}; "
                    "keeping the current timeline",
                    flush=True,
                )
                return
            frame_times = [latest]
        else:
            latest_published = max(published)
            frame_times = [latest_published + FRAME_STEP * index for index in range(1, count + 1)]

    rolling = published is not None and not explicit_start and not bootstrap

    revision = format(int(time.time()), "x")[-8:]
    output_root = support.RESULTS / "v2-live"
    output_root.mkdir(parents=True, exist_ok=True)
    print(f"Building {len(frame_times)} frame(s), {frame_times[0].isoformat()} through "
          f"{frame_times[-1].isoformat()}, z{mosaic.MIN_ZOOM}–{mosaic.MAX_ZOOM}, AVIF quality {quality}, "
          f"{workers} render workers", flush=True)

    started = time.perf_counter()
    built: dict[dt.datetime, dict] = {}
    uploaded_bytes = 0

    def build(when: dt.datetime) -> None:
        nonlocal uploaded_bytes
        built[when], size = build_frame(token, when, revision, output_root, quality, workers)
        uploaded_bytes += size

    if rolling:
        wait_for_inputs(frame_times[-1], dt.datetime.now(UTC) + dt.timedelta(minutes=max_wait))
    elif explicit_start:
        wait_for_inputs(frame_times[-1], frame_times[-1] + dt.timedelta(minutes=max_wait))
    for when in frame_times:
        build(when)

    if rolling:
        # Wait for another run's frame before attempting to take over its slot.
        deadline = dt.datetime.now(UTC) + dt.timedelta(minutes=max_wait + owner_timeout)
        frame_ids = complete_timeline(token, frame_times[-1], deadline, built, build)
    elif bootstrap:
        frames = [built[frame_times[-1]]]
        publish_manifest(token, frames)
        frame_ids = [frame["id"] for frame in frames]
    else:
        merged = {**(published or {}), **built}
        frames = [merged[when] for when in sorted(merged)][-MAX_FRAME_COUNT:]
        publish_manifest(token, frames, allow_rewind=explicit_start)
        frame_ids = [frame["id"] for frame in frames]
    print(json.dumps({
        "frame_ids": frame_ids,
        "new_frames": len(built),
        "uploaded_mb": round(uploaded_bytes / 1e6, 1),
        "wall_seconds": round(time.perf_counter() - started, 1),
        "peak_rss_mb": support.peak_rss_mb(),
    }, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
