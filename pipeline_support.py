#!/usr/bin/env python3
"""Shared input and runtime helpers for the satellite publisher."""

from __future__ import annotations

import datetime as dt
import os
import platform
import resource
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("SATELLITE_RUNTIME_DIR") or Path(os.environ.get("RUNNER_TEMP", ROOT)) / "satellite-pipeline")
CACHE = RUNTIME_DIR / ".cache"
RESULTS = RUNTIME_DIR / "results"
USER_AGENT = "SatellitePipeline/2.0"
S3_NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
INPUT_CACHE_IDLE_SECONDS = 30 * 60


def parse_utc(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024**2 if platform.system() == "Darwin" else 1024
    return round(peak / divisor, 2)


def list_bucket(bucket: str, prefix: str) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    token = None
    while True:
        query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            query["continuation-token"] = token
        url = f"https://{bucket}.s3.amazonaws.com/?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            root = ET.fromstring(response.read())
        items.extend(
            (node.findtext("s:Key", namespaces=S3_NS), int(node.findtext("s:Size", namespaces=S3_NS)))
            for node in root.findall("s:Contents", namespaces=S3_NS)
        )
        if root.findtext("s:IsTruncated", namespaces=S3_NS) != "true":
            return items
        token = root.findtext("s:NextContinuationToken", namespaces=S3_NS)
        if not token:
            raise RuntimeError(f"S3 listing for {bucket}/{prefix} was truncated without a continuation token")


def download_object(obj: dict) -> dict:
    path = CACHE / "inputs" / obj["satellite"] / obj["key"].rsplit("/", 1)[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size == obj["size"]:
        os.utime(path)
        return {**obj, "path": str(path), "download_seconds": 0.0, "downloaded_bytes": 0, "cached": True}

    temporary = path.with_suffix(path.suffix + ".part")
    url = f"https://{obj['bucket']}.s3.amazonaws.com/{obj['key']}"
    started = perf_counter()
    error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                while chunk := response.read(4 * 1024 * 1024):
                    output.write(chunk)
            if temporary.stat().st_size != obj["size"]:
                raise IOError(f"size mismatch: expected {obj['size']}, got {temporary.stat().st_size}")
            temporary.replace(path)
            return {
                **obj,
                "path": str(path),
                "download_seconds": round(perf_counter() - started, 4),
                "downloaded_bytes": obj["size"],
                "cached": False,
            }
        except Exception as exc:
            error = exc
            temporary.unlink(missing_ok=True)
            time.sleep(1 + attempt)
    raise RuntimeError(f"Failed to download {url}: {error}")


def cleanup_input_cache(cache_root: Path = CACHE,
                        idle_seconds: float = INPUT_CACHE_IDLE_SECONDS) -> tuple[int, int]:
    cutoff = time.time() - idle_seconds
    input_root = cache_root / "inputs"
    if not input_root.is_dir() or input_root.is_symlink():
        return 0, 0

    removed_files = 0
    removed_bytes = 0
    for path in input_root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                stat = path.stat()
                if stat.st_mtime > cutoff:
                    continue
                removed_bytes += stat.st_size
                path.unlink()
                removed_files += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                print(f"Could not remove cached input {path}: {exc}", flush=True)
    for path in sorted(input_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                pass
    return removed_files, removed_bytes
