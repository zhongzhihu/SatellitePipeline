"""Publish satellite packs and the manifest directly to Cloudflare R2 (S3 API)."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

import pipeline_support as support

PART_BYTES = 48 * 1024 * 1024  # R2: equal parts ≥ 5 MiB except the last
PACK_RETENTION = dt.timedelta(hours=3)


class ManifestConflict(RuntimeError):
    """The published manifest is newer than ours or changed since we read it."""


def _required_environment() -> tuple[str, str, str, str]:
    names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    values = [os.environ.get(name, "").strip() for name in names]
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError("Missing R2 environment variable(s): " + ", ".join(missing))
    return tuple(values)


def _latest(frames: list[dict]) -> dt.datetime | None:
    times = [support.parse_utc(frame["valid_time"]) for frame in frames if frame.get("valid_time")]
    return max(times, default=None)


class R2Store:
    def __init__(self, client, bucket: str, prefix: str = "") -> None:
        self.client = client
        self.bucket = bucket
        # Must match the Worker's SATELLITE_STORAGE_PREFIX; empty is the bucket root.
        self.prefix = prefix.strip("/")

    @classmethod
    def from_environment(cls) -> R2Store:
        account_id, access_key, secret_key, bucket = _required_environment()
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            region_name="auto",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                retries={"mode": "adaptive", "max_attempts": 10},
                # R2 does not accept every default checksum newer botocore sends.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return cls(client, bucket, os.environ.get("SATELLITE_STORAGE_PREFIX", ""))

    def key(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def pack_key(self, frame_id: str, revision: str) -> str:
        return self.key(f"packs/{frame_id}-{revision}.pack")

    def upload_pack(self, frame_id: str, revision: str, path: Path) -> int:
        """Upload a pack (multipart above one part); a failed upload is aborted."""
        self.client.upload_file(
            str(path), self.bucket, self.pack_key(frame_id, revision),
            ExtraArgs={"ContentType": "application/octet-stream"},
            Config=TransferConfig(multipart_threshold=PART_BYTES, multipart_chunksize=PART_BYTES, max_concurrency=4),
        )
        return path.stat().st_size

    def read_manifest(self) -> tuple[dict | None, str | None]:
        """The published manifest and its ETag, or ``(None, None)``."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key("manifest.json"))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None, None
            raise
        return json.loads(response["Body"].read()), response["ETag"]

    def publish_manifest(self, manifest: dict, allow_rewind: bool = False) -> None:
        """Replace the manifest unless another run published a newer or concurrent one.

        Overlapping runs finish out of order, so a slow run must not roll the
        timeline back; deliberate backfills pass ``allow_rewind``. The write is
        conditional on the manifest we read, so a run that publishes between
        our read and write raises ``ManifestConflict`` instead of being lost.
        """
        previous, etag = self.read_manifest()
        previous_frames = (previous or {}).get("frames", [])
        same_grid = previous is not None and all(
            previous.get(key) == manifest[key] for key in ("minimum_zoom_level", "maximum_zoom_level")
        )
        previous_latest = _latest(previous_frames)
        if same_grid and not allow_rewind and previous_latest and previous_latest > _latest(manifest["frames"]):
            raise ManifestConflict(f"Manifest is older than the published timeline ({previous_latest.isoformat()})")
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=self.key("manifest.json"),
                Body=json.dumps(manifest, separators=(",", ":")).encode(),
                ContentType="application/json; charset=utf-8", CacheControl="no-store", **condition,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}:
                raise ManifestConflict("Manifest changed while publishing") from error
            raise
        # Clients with a briefly cached previous manifest still fetch its packs.
        keep = {self.pack_key(frame["id"], frame["pack"]) for frame in [*manifest["frames"], *previous_frames]
                if frame.get("id") and frame.get("pack")}
        try:
            self.delete_stale_packs(keep)
        except Exception as error:  # the next publication retries
            print(f"Pack cleanup failed: {error}", flush=True)

    def delete_stale_packs(self, keep: set[str], now: dt.datetime | None = None) -> list[str]:
        cutoff = (now or dt.datetime.now(dt.timezone.utc)) - PACK_RETENTION
        stale = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.key("packs/")):
            stale += [item["Key"] for item in page.get("Contents", [])
                      if item["Key"] not in keep and item["LastModified"] < cutoff]
        for start in range(0, len(stale), 1000):
            response = self.client.delete_objects(Bucket=self.bucket, Delete={
                "Objects": [{"Key": key} for key in stale[start:start + 1000]], "Quiet": True,
            })
            if response.get("Errors"):
                raise RuntimeError(f"Could not delete {len(response['Errors'])} stale pack(s): {response['Errors'][0]}")
        return stale
