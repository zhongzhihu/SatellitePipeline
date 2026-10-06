"""Checks for direct R2 manifest publication and pack retention."""
import datetime as dt
import io
import json
import unittest

from botocore.exceptions import ClientError

from frame_schedule import validate_timeline
from r2_store import ManifestConflict, R2Store

UTC = dt.timezone.utc


def frame(when: str, pack: str = "abc123") -> dict:
    valid = dt.datetime.fromisoformat(when).replace(tzinfo=UTC)
    return {"id": valid.strftime("%Y%m%dT%H%MZ"), "valid_time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pack": pack, "pack_bytes": 1000}


def manifest(*frames: dict) -> dict:
    return {"minimum_zoom_level": 1, "maximum_zoom_level": 5, "frames": list(frames)}


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Operation")


class FakeS3:
    """An in-memory bucket with S3 conditional-write semantics."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, dt.datetime]] = {}
        self.version = 0
        self.deleted: list[str] = []

    def add(self, key: str, body: bytes = b"", modified: dt.datetime | None = None) -> None:
        self.version += 1
        self.objects[key] = (body, f'"{self.version}"', modified or dt.datetime.now(UTC))

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise client_error("NoSuchKey")
        body, etag, _ = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": etag}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **_):
        current = self.objects.get(Key)
        if (IfNoneMatch == "*" and current) or (IfMatch and (not current or current[1] != IfMatch)):
            raise client_error("PreconditionFailed")
        self.add(Key, Body)

    def get_paginator(self, name):
        objects = self.objects

        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": key, "LastModified": modified}
                                    for key, (_, _, modified) in objects.items() if key.startswith(Prefix)]}
        return Paginator()

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"])
            self.deleted.append(item["Key"])
        return {}


class R2StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s3 = FakeS3()
        self.store = R2Store(self.s3, "bucket", "/custom/")

    def published(self) -> dict:
        return json.loads(self.s3.objects["custom/manifest.json"][0])

    def test_first_publication_creates_manifest_under_prefix(self):
        self.store.publish_manifest(manifest(frame("2026-10-04T22:00")))
        self.assertEqual(self.published()["frames"][0]["id"], "20261004T2200Z")

    def test_older_timeline_is_rejected_unless_rewinding(self):
        self.store.publish_manifest(manifest(frame("2026-10-04T22:10")))
        with self.assertRaises(ManifestConflict):
            self.store.publish_manifest(manifest(frame("2026-10-04T22:00")))
        self.store.publish_manifest(manifest(frame("2026-10-04T22:00")), allow_rewind=True)
        self.assertEqual(self.published()["frames"][0]["id"], "20261004T2200Z")

    def test_concurrent_write_between_read_and_write_conflicts(self):
        self.store.publish_manifest(manifest(frame("2026-10-04T22:00")))
        read = self.store.read_manifest
        def read_then_race():
            result = read()
            self.s3.add("custom/manifest.json", json.dumps(manifest(frame("2026-10-04T22:20"))).encode())
            return result
        self.store.read_manifest = read_then_race
        with self.assertRaises(ManifestConflict):
            self.store.publish_manifest(manifest(frame("2026-10-04T22:10")))
        self.assertEqual(self.published()["frames"][0]["id"], "20261004T2220Z")

    def test_cleanup_keeps_current_previous_and_recent_packs(self):
        old = dt.datetime.now(UTC) - dt.timedelta(hours=4)
        for name in ("20261004T2200Z-a", "20261004T1800Z-c"):
            self.s3.add(f"custom/packs/{name}.pack", modified=old)
        self.s3.add("custom/packs/20261004T2210Z-b.pack")  # recent but not yet referenced
        self.store.publish_manifest(manifest(frame("2026-10-04T22:00", "a")))
        self.store.publish_manifest(manifest(frame("2026-10-04T22:10", "b")))
        self.assertEqual(self.s3.deleted, ["custom/packs/20261004T1800Z-c.pack"])
        self.store.publish_manifest(manifest(frame("2026-10-04T22:20", "e")))
        self.assertEqual(self.s3.deleted[1:], ["custom/packs/20261004T2200Z-a.pack"])


class TimelineValidationTests(unittest.TestCase):
    def test_consecutive_frames_are_accepted(self):
        validate_timeline([frame("2026-10-04T22:00"), frame("2026-10-04T22:10")])

    def test_invalid_timelines_are_rejected(self):
        bad_id = {**frame("2026-10-04T22:00"), "id": "20261004T2210Z"}
        for frames in ([], [frame("2026-10-04T22:00"), frame("2026-10-04T22:20")], [bad_id],
                       [frame("2026-10-04T22:00", "Bad/Pack")], [frame("2026-10-04T22:05")]):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                validate_timeline(frames)


if __name__ == "__main__":
    unittest.main()
