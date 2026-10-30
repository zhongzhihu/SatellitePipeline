"""Regressions for authenticated Meteosat acquisition and calibration."""
import datetime as dt
import hashlib
import io
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import eumetsat_store as store
import mosaic_v2 as mosaic

WHEN = dt.datetime(2026, 10, 30, 6, 40, tzinfo=dt.timezone.utc)


def fci_names():
    return [f"W_XX-MTI1+FCI-1C-RRAD-HRFI-FD--CHK-BODY---NC4E_C_0041_{i:04d}.nc"
            for i in range(1, 41)]


def feature(start, entries, product_id="scan", md5=None):
    return {"id": product_id, "properties": {
        "date": f"{start}/2026-10-30T06:49:28Z",
        "links": {"sip-entries": [{"title": entry} for entry in entries]},
        "extraInformation": {"md5": md5},
    }}


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as out:
        for i, entry in enumerate(entries):
            out.writestr("product/" + entry, f"image data {i}".encode())
    return buffer.getvalue()


class SearchTests(unittest.TestCase):
    def test_selection_uses_nominal_time_and_rejects_future_incomplete_and_old_scans(self):
        payload = {"features": [
            feature("2026-10-30T06:50:07Z", fci_names(), "future"),
            feature("2026-10-30T06:40:07Z", fci_names()[:-1], "incomplete"),
            feature("2026-10-30T06:30:07Z", fci_names(), "earlier"),
            feature("2026-10-30T06:00:07Z", fci_names(), "too-old"),
            feature("2026-10-30T06:40:07Z", ["quicklook.jpg"], "quicklook"),
            feature("2026-10-30T06:40:07Z", fci_names(), "complete"),
        ]}
        result = store.products_from_search("MTG", WHEN, payload)
        self.assertEqual([p.product_id for p in result], ["complete", "earlier"])
        self.assertEqual(result[0].timestamp, WHEN)
        self.assertEqual(len(result[0].entries), 40)

    def test_rejects_duplicate_or_wrong_scan_chunks(self):
        with self.assertRaisesRegex(store.StoreError, "duplicate"):
            store._data_entries("MTG", fci_names() + [fci_names()[0]])
        with self.assertRaisesRegex(store.StoreError, "incomplete"):
            store._data_entries("MTG", [x.replace("HRFI", "FDHSI") for x in fci_names()])

    def test_iodc_start_is_floored_to_15_minutes_and_age_is_bounded(self):
        entries = ["MSG2-SEVI-MSG15-0100-NA-20261030064240.622000000Z-NA.nat"]
        payload = {"features": [feature("2026-10-30T06:30:10.780Z", entries, "latest"),
                                feature("2026-10-30T05:00:10Z", entries, "old")]}
        result = store.products_from_search("IODC", WHEN, payload)
        self.assertEqual([p.product_id for p in result], ["latest"])
        self.assertEqual(result[0].timestamp, WHEN - dt.timedelta(minutes=10))


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        for pos in range(0, len(self.body), 31):
            yield self.body[pos:pos + 31]


class DownloadTests(unittest.TestCase):
    def test_verified_archive_cache_and_recovery_from_truncated_cached_file(self):
        entries = fci_names()
        content = archive(entries)
        product = store.Product("MTG", "scan", WHEN, tuple(entries), hashlib.md5(content).hexdigest())
        client = store.DataStore(("key", "secret"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "_get", side_effect=lambda *a, **k: FakeResponse(content)) as get:
            paths = client.download(product, time.monotonic() + 30, Path(tmp))
            self.assertEqual(len(paths), 40)
            self.assertTrue(all(Path(path).is_file() for path in paths))
            client.download(product, time.monotonic() + 30, Path(tmp))
            self.assertEqual(get.call_count, 1)
            Path(paths[0]).write_bytes(b"truncated")
            client.download(product, time.monotonic() + 30, Path(tmp))
            self.assertEqual(get.call_count, 2)
            self.assertEqual(Path(paths[0]).read_bytes(), b"image data 0")

    def test_wrong_checksum_never_promotes_partial_data(self):
        entries = fci_names()
        content = archive(entries)
        product = store.Product("MTG", "scan", WHEN, tuple(entries), "0" * 32)
        client = store.DataStore(("key", "secret"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "_get", side_effect=lambda *a, **k: FakeResponse(content)) as get:
            with self.assertRaisesRegex(store.StoreError, "checksum mismatch"):
                client.download(product, time.monotonic() + 30, Path(tmp))
            self.assertEqual(get.call_count, 2)
            self.assertFalse(list(Path(tmp).rglob("complete.json")))
            self.assertFalse(list(Path(tmp).rglob("*.partial")))

    def test_archive_missing_image_is_rejected_even_if_checksum_matches(self):
        entries = fci_names()
        content = archive(entries[:-1])
        product = store.Product("MTG", "scan", WHEN, tuple(entries), hashlib.md5(content).hexdigest())
        client = store.DataStore(("key", "secret"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "_get", return_value=FakeResponse(content)):
            with self.assertRaisesRegex(store.StoreError, "missing image files"):
                client.download(product, time.monotonic() + 30, Path(tmp))
            self.assertFalse(list(Path(tmp).rglob("complete.json")))

    def test_token_and_download_authorization_rejection_are_not_retried(self):
        response = Mock(status_code=403)
        client = store.DataStore(("key", "secret"))
        with patch.object(client, "_access_token", return_value="private-token"), \
                patch.object(store.requests, "get", return_value=response) as request:
            with self.assertRaisesRegex(store.StoreAuthError, "403"):
                client._get("https://api.eumetsat.int/example", time.monotonic() + 30)
            self.assertEqual(request.call_count, 1)
            self.assertNotIn("private-token", str(request.call_args.args))


class PipelineTests(unittest.TestCase):
    def test_earlier_product_is_tried_after_a_download_failure(self):
        first = store.Product("MTG", "first", WHEN, tuple(fci_names()), None)
        second = store.Product("MTG", "second", WHEN - dt.timedelta(minutes=10), tuple(fci_names()), None)
        client = Mock()
        client.search.return_value = [first, second]
        client.download.side_effect = [store.StoreError("offline"), ["ok.nc"]]
        product, paths, _ = mosaic.download_store_source("MTG", WHEN, client)
        self.assertIs(product, second)
        self.assertEqual(paths, ["ok.nc"])

    def test_failed_visible_decode_preserves_complete_infrared_and_actual_time(self):
        ir = mosaic.NativeLayer(np.full((2, 2), 100, np.uint8), None, 0, 1, 1, 1)
        product = store.Product("MTG", "scan", WHEN - dt.timedelta(minutes=10), tuple(fci_names()), None)
        with patch.object(mosaic, "load_native_layer", side_effect=[(ir, {"loaded": True}), RuntimeError("bad VIS")]) as decode:
            infrared, visible, info = mosaic.decode_store_source("MTG", WHEN, (product, ["data"], 1.0))
        self.assertIs(infrared, ir)
        self.assertIsNone(visible)
        self.assertEqual(info["fallback_minutes"], 10)
        self.assertEqual(info["timestamp"], "2026-10-30T06:30:00Z")
        self.assertEqual(decode.call_args.kwargs["stride"], 2)

    def test_store_acquisition_uses_view_only_for_failed_source(self):
        product = store.Product("MTG", "scan", WHEN, tuple(fci_names()), None)
        ir = mosaic.NativeLayer(np.full((2, 2), 100, np.uint8), None, 0, 1, 1, 1)
        result = (ir, None, {"timestamp": "2026-10-30T06:40:00Z", "backend": "EUMETSAT Data Store"})
        def download(name, when, client):
            return (product, ["file"], 1) if name == "MTG" else None
        with patch.object(store, "credentials_from_environment", return_value=("key", "secret")), \
             patch.object(mosaic, "configure_fci_decompression"), \
             patch.object(mosaic, "load_native_layers", return_value={}), \
             patch.object(mosaic, "download_store_source", side_effect=download), \
             patch.object(mosaic, "decode_store_source", return_value=result), \
             patch.object(mosaic, "fetch_view_sources", return_value={}) as fallback:
            sources = mosaic.acquire_sources(WHEN)
        fallback.assert_called_once_with(["IODC"], WHEN)
        mtg = next(s for s in sources if s.name == "MTG")
        self.assertIs(mtg.ir, ir)
        self.assertEqual(mtg.time, WHEN)
        self.assertEqual(mtg.info["backend"], "EUMETSAT Data Store")

    def test_view_sources_have_separate_deadlines_and_run_concurrently(self):
        barrier = threading.Barrier(3, timeout=2)
        def fetch(name, when, deadline):
            barrier.wait()
            return None, None, {"available": False}
        with patch.object(mosaic, "fetch_wcs_source", side_effect=fetch):
            result = mosaic.fetch_view_sources(["MTG", "IODC", "FES"], WHEN)
        self.assertEqual(set(result), {"MTG", "IODC", "FES"})

    def test_fci_north_up_and_strided_pixel_centres_are_preserved(self):
        import dask.array as da
        import pyproj
        import xarray as xr
        from types import SimpleNamespace

        area = SimpleNamespace(area_extent=(-2000., -2000., 2000., 2000.),
                               pixel_size_x=1000., pixel_size_y=1000.,
                               crs=pyproj.CRS.from_dict({"proj": "geos", "h": 35786400., "lon_0": 0}))
        values = np.array([[0, 10, 20, 30], [40, 50, 60, 70],
                           [80, 90, 100, 110], [120, 130, 140, 150]], dtype=np.float32)
        data = xr.DataArray(da.from_array(values, chunks=(2, 2)), dims=("y", "x"), attrs={"area": area})
        # MagicMock supports Satpy's subscript access.
        from unittest.mock import MagicMock
        scene = MagicMock()
        scene.__getitem__.return_value = data
        with patch("satpy.Scene", return_value=scene), patch.object(mosaic, "configure_fci_decompression"):
            layer, info = mosaic.load_native_layer(["synthetic.nc"], "fci_l1c_nc", "vis_06", "vis", stride=2)
        scene.load.assert_called_once_with(["vis_06"], upper_right_corner="NE")
        np.testing.assert_array_equal(layer.codes, mosaic.reflectance_code(values[::2, ::2] / 100.0))
        self.assertEqual((layer.x0, layer.y0, layer.dx, layer.dy), (-2500., 2500., 2000., 2000.))
        self.assertEqual(info["native_shape"], [4, 4])


if __name__ == "__main__":
    unittest.main()
