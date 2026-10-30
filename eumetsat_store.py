"""Bounded, authenticated access to EUMETSAT Data Store native products.

The Data Store's OpenSearch and download endpoints are also used by EUMDAC.
We use them directly to give every HTTP operation a timeout and a common
per-source deadline; the EUMDAC Python API does not expose request timeouts.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

import pipeline_support as support

UTC = dt.timezone.utc
API = "https://api.eumetsat.int"
SEARCH_URL = f"{API}/data/search-products/1.0.0/os"
DOWNLOAD_URL = f"{API}/data/download/1.0.0/collections"
COLLECTIONS = {"MTG": "EO:EUM:DAT:0665", "IODC": "EO:EUM:DAT:MSG:HRSEVIRI-IODC"}
CADENCE = {"MTG": 10, "IODC": 15}
MAX_AGE_MINUTES = {"MTG": 30, "IODC": 75}
BODY_NUMBER = re.compile(r"_(\d{4})\.nc$")


class StoreError(RuntimeError):
    pass


class StoreAuthError(StoreError):
    pass


@dataclass(frozen=True)
class Product:
    name: str
    product_id: str
    timestamp: dt.datetime  # nominal scan start, aligned to the instrument cadence
    entries: tuple[str, ...]
    md5: str | None


def _nominal_time(text: str, cadence: int) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.replace(minute=value.minute - value.minute % cadence, second=0, microsecond=0)


def _data_entries(name: str, entries: list[str]) -> tuple[str, ...]:
    """Require every FCI image chunk, or exactly one SEVIRI native file."""
    if name == "MTG":
        selected: dict[int, str] = {}
        for entry in entries:
            base = Path(entry).name
            match = BODY_NUMBER.search(base)
            if not match or "FCI-1C-RRAD-HRFI-FD--" not in base or "-BODY-" not in base:
                continue
            number = int(match.group(1))
            if number in selected:
                raise StoreError(f"duplicate MTG chunk {number}")
            selected[number] = base
        missing = set(range(1, 41)) - selected.keys()
        if missing or set(selected) != set(range(1, 41)):
            raise StoreError(f"incomplete MTG product: {len(selected)}/40 image chunks")
        return tuple(selected[n] for n in range(1, 41))
    selected = [Path(entry).name for entry in entries if entry.lower().endswith(".nat")]
    if len(selected) != 1:
        raise StoreError(f"IODC product contains {len(selected)} native image files")
    return tuple(selected)


def products_from_search(name: str, when: dt.datetime, payload: dict) -> list[Product]:
    """Pick complete products at or before the requested nominal frame time."""
    candidates = []
    for feature in payload.get("features", []):
        try:
            props = feature["properties"]
            stamp = _nominal_time(props["date"].split("/")[0], CADENCE[name])
            age = when.astimezone(UTC) - stamp
            if age < dt.timedelta(0) or age > dt.timedelta(minutes=MAX_AGE_MINUTES[name]):
                continue
            titles = [entry["title"] for entry in props["links"]["sip-entries"]]
            entries = _data_entries(name, titles)
            checksum = (props.get("extraInformation") or {}).get("md5")
            if checksum and not re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
                raise StoreError("invalid product checksum")
            candidates.append(Product(name, feature["id"], stamp, entries,
                                      checksum.lower() if checksum else None))
        except (KeyError, TypeError, ValueError, StoreError) as exc:
            print(f"{name} Data Store: ignoring unusable product metadata ({exc})", flush=True)
    return sorted(candidates, key=lambda item: item.timestamp, reverse=True)


def credentials_from_environment() -> tuple[str, str] | None:
    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    if bool(key) != bool(secret):
        print("EUMETSAT Data Store requires both consumer key and consumer secret; using EUMETView", flush=True)
    return (key, secret) if key and secret else None


def _timeout(deadline: float) -> tuple[float, float]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StoreError("Data Store download deadline exceeded")
    return min(10.0, remaining), min(60.0, remaining)


class DataStore:
    def __init__(self, credentials: tuple[str, str]):
        self.credentials = credentials
        self._token = ""
        self._expires = 0.0
        self._token_lock = threading.Lock()

    def _access_token(self, deadline: float) -> str:
        with self._token_lock:
            if self._token and time.monotonic() < self._expires - 60:
                return self._token
            try:
                response = requests.post(
                    f"{API}/token", auth=self.credentials,
                    data={"grant_type": "client_credentials", "validity_period": 86400},
                    timeout=_timeout(deadline),
                )
                if response.status_code in (401, 403):
                    raise StoreAuthError(f"token request rejected (HTTP {response.status_code})")
                response.raise_for_status()
                body = response.json()
                self._token = body["access_token"]
                self._expires = time.monotonic() + float(body["expires_in"])
                return self._token
            except StoreError:
                raise
            except Exception as exc:
                # Do not print request details, headers, or token responses.
                raise StoreError(f"token request failed ({type(exc).__name__})") from exc

    def _get(self, url: str, deadline: float, **kwargs):
        error = None
        for attempt in range(3):
            try:
                response = requests.get(
                    url, headers={"Authorization": f"Bearer {self._access_token(deadline)}",
                                  "User-Agent": support.USER_AGENT,
                                  "Accept-Encoding": "identity"},
                    timeout=_timeout(deadline), **kwargs,
                )
                if response.status_code in (401, 403):
                    response.close()
                    raise StoreAuthError(f"Data Store access rejected (HTTP {response.status_code}); check credentials and licence")
                if response.status_code == 429 or response.status_code >= 500:
                    error = f"HTTP {response.status_code}"
                    response.close()
                else:
                    response.raise_for_status()
                    return response
            except StoreError:
                raise
            except requests.RequestException as exc:
                error = type(exc).__name__
            if attempt < 2:
                delay = min(2 ** attempt, max(0.0, deadline - time.monotonic()))
                if delay:
                    time.sleep(delay)
        raise StoreError(f"Data Store request failed ({error})")

    def search(self, name: str, when: dt.datetime, deadline: float) -> list[Product]:
        # Include the nominal scan's first seconds and filter on its start time
        # ourselves: OpenSearch returns products overlapping the query interval.
        start = when - dt.timedelta(minutes=MAX_AGE_MINUTES[name] + CADENCE[name])
        end = when + dt.timedelta(minutes=CADENCE[name])
        with self._get(SEARCH_URL, deadline, params={
            "pi": COLLECTIONS[name], "format": "json", "c": 100,
            "dtstart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dtend": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }) as response:
            payload = response.json()
        if int(payload.get("totalResults", 0)) > 100:
            raise StoreError(f"{name} Data Store search returned more than 100 results")
        return products_from_search(name, when, payload)

    def download(self, product: Product, deadline: float, cache_root: Path | None = None) -> list[str]:
        root = (cache_root or support.CACHE) / "inputs" / "EUMETSAT" / product.name
        # Product IDs are too long for some filesystems; hash also prevents path traversal.
        folder = root / hashlib.sha256(product.product_id.encode()).hexdigest()
        marker = folder / "complete.json"
        try:
            metadata = json.loads(marker.read_text())
            sizes = metadata["sizes"]
            if metadata["md5"] == product.md5 and set(sizes) == set(product.entries) and all(
                (folder / name).is_file() and (folder / name).stat().st_size == sizes[name]
                for name in product.entries
            ):
                for name in product.entries:
                    os.utime(folder / name)
                os.utime(marker)
                return [str(folder / name) for name in product.entries]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        root.mkdir(parents=True, exist_ok=True)
        staging = root / (folder.name + ".partial")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        archive = staging / "product.zip"
        collection = urllib.parse.quote(COLLECTIONS[product.name], safe="")
        product_id = urllib.parse.quote(product.product_id, safe="")
        url = f"{DOWNLOAD_URL}/{collection}/products/{product_id}"
        try:
            error = None
            for attempt in range(2):
                try:
                    checksum = hashlib.md5()  # EUMETSAT publishes an MD5 for the complete SIP.
                    total = 0
                    with self._get(url, deadline, stream=True) as response, archive.open("wb") as out:
                        length = response.headers.get("Content-Length")
                        for block in response.iter_content(chunk_size=1024 * 1024):
                            if time.monotonic() >= deadline:
                                raise StoreError("Data Store download deadline exceeded")
                            if block:
                                out.write(block)
                                checksum.update(block)
                                total += len(block)
                    if length is not None and total != int(length):
                        raise StoreError(f"short download: expected {length} bytes, got {total}")
                    if product.md5 and checksum.hexdigest() != product.md5:
                        raise StoreError("product archive checksum mismatch")
                    break
                except StoreAuthError:
                    raise
                except (StoreError, requests.RequestException, OSError, ValueError) as exc:
                    error = str(exc) if isinstance(exc, StoreError) else type(exc).__name__
                    archive.unlink(missing_ok=True)
                    if attempt == 1 or time.monotonic() >= deadline:
                        raise StoreError(f"{product.name} download failed: {error}") from exc
                    print(f"{product.name} Data Store retrying interrupted download ({error})", flush=True)
            with zipfile.ZipFile(archive) as zf:
                members = {}
                for info in zf.infolist():
                    base = Path(info.filename).name
                    if base in product.entries:
                        if base in members:
                            raise StoreError(f"duplicate archive entry {base}")
                        members[base] = info
                if set(members) != set(product.entries):
                    raise StoreError(f"{product.name} archive is missing image files")
                sizes = {}
                for name in product.entries:
                    with zf.open(members[name]) as src, (staging / name).open("wb") as dest:
                        shutil.copyfileobj(src, dest, length=1024 * 1024)
                    # ZipFile checks each file's CRC as it is read.
                    sizes[name] = members[name].file_size
            archive.unlink()
            (staging / "complete.json").write_text(json.dumps({"md5": product.md5, "sizes": sizes}))
            shutil.rmtree(folder, ignore_errors=True)
            staging.replace(folder)
            return [str(folder / name) for name in product.entries]
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
