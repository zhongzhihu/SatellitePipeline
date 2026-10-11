"""Offline regressions for source fallback, acquisition, and empty frames."""

import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import mosaic_v2 as m
import publish_live as publisher

WHEN = dt.datetime(2026, 10, 3, 17, tzinfo=m.UTC)


def layer(code):
    return m.NativeLayer(np.full((180, 360), code, np.uint8), None, -180, 90, 1, 1)


class ChannelFallbackTests(unittest.TestCase):
    def setUp(self):
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in (("BLOCK_SIZE", 32), ("NODE_STEP", 4),
                            ("_UPSAMPLE", m.upsample_matrix(32, 4)), ("_WHEN", WHEN)):
            self.patches.enter_context(patch.object(m, name, value))

    def render(self, sources):
        with patch.object(m, "_SOURCES", sources):
            return m.render_block((2, 4), world_px=256)

    def test_vis_only_primary_does_not_suppress_backup_ir(self):
        backup = m.Source("G18", "backup", -137, 0, ir=layer(100), time=WHEN)
        primary = m.Source("G19", "primary", -75.2, 0, vis=layer(200), time=WHEN)
        expected = self.render([backup])
        actual = self.render([backup, primary])
        self.assertTrue(expected["ir"].any())
        np.testing.assert_array_equal(actual["ir"], expected["ir"])
        self.assertTrue(actual["vis"].any())

    def test_ir_only_primary_does_not_suppress_backup_vis(self):
        backup = m.Source("G18", "backup", -137, 0, vis=layer(200), time=WHEN)
        primary = m.Source("G19", "primary", -75.2, 0, ir=layer(180), time=WHEN)
        expected = self.render([backup])
        actual = self.render([backup, primary])
        self.assertTrue(expected["vis"].any())
        np.testing.assert_array_equal(actual["vis"], expected["vis"])
        self.assertTrue(actual["ir"].any())

    def test_healthy_channels_keep_the_existing_softmax_blend(self):
        backup = m.Source("G18", "backup", -137, 0, ir=layer(100), vis=layer(200), time=WHEN)
        primary = m.Source("G19", "primary", -75.2, 0, ir=layer(180), vis=layer(220), time=WHEN)

        def view(lon, lat, satellite_lon):
            return np.full(lon.shape, .75 if satellite_lon == -137 else .8, np.float32)

        with patch.object(m, "view_cosine", side_effect=view), \
                patch.object(m, "cos_solar_zenith", side_effect=lambda lon, *args: np.ones(lon.shape, np.float32)):
            actual = self.render([backup, primary])
        weight = np.exp((.75 - .8) / m.SOFTMAX_TAU)
        self.assertTrue(np.all(actual["ir"] == round((100 * weight + 180) / (weight + 1))))
        self.assertTrue(np.all(actual["vis"] == round((200 * weight + 220) / (weight + 1))))


class WcsNodataTests(unittest.TestCase):
    def test_all_wcs_luts_preserve_zero_and_keep_nonzero_data_valid(self):
        luts = [*m.WCS_IR_LUTS.values(), m.VIS_GRAY_LUT, m.MTG_VIS_GRAY_LUT]
        for lut in luts:
            with self.subTest(lut=lut):
                self.assertEqual(int(lut[0]), 0)
                self.assertTrue(np.all(lut[1:] > 0))

    def test_unchunked_coverage_preserves_missing_pixels(self):
        gray = np.array([[0, 100], [150, 200]], np.uint8)
        with patch.object(m, "wcs_get", return_value=b"offline"), \
                patch.object(m, "decode_geotiff", return_value=(gray, -1, 1, 1, 1)):
            for lut in (m.WCS_IR_LUTS["FES"], m.VIS_GRAY_LUT):
                result = m.fetch_wcs_coverage("FES", "offline", WHEN, lut, None, 999)
                self.assertEqual(int(result.codes[0, 0]), 0)
                self.assertTrue(np.all(result.codes[gray > 0] > 0))

    def test_chunked_coverage_preserves_missing_pixels(self):
        source = ("ir", "vis", 10, (-2., 2.), (-2., 2.), 2., None)

        def chunk(coverage, when, lat, lon, scale, deadline):
            return np.array([[0, 100], [100, 100]], np.uint8), lon[0], lat[1], 1., 1.

        with patch.dict(m.WCS_SOURCES, MTG=source), patch.object(m, "wcs_get", side_effect=chunk), \
                patch.object(m, "decode_geotiff", side_effect=lambda result: result):
            result = m.fetch_wcs_coverage("MTG", "ir", WHEN, m.WCS_IR_LUTS["MTG"], None, 999)
        self.assertEqual(np.count_nonzero(result.codes == 0), 4)
        self.assertEqual(np.count_nonzero(result.codes > 0), 12)


class NativeRetryTests(unittest.TestCase):
    key = ("H09", "B13")

    def download(self, count):
        return {self.key: [{"path": f"segment{i}"} for i in range(count)]}, {self.key}

    def test_partial_channel_is_retried_and_completed(self):
        partial, complete = layer(100), layer(150)
        with patch.object(m, "download_native", side_effect=[self.download(9), self.download(10)]) as download, \
                patch.object(m, "load_native_layer", side_effect=[(partial, {}), (complete, {})]):
            result = m.load_native_layers(WHEN)
        self.assertIs(result[self.key][0], complete)
        self.assertEqual(download.call_args_list[1].args, (WHEN, {self.key}))

    def test_persistent_partial_is_accepted_after_one_retry(self):
        partial = layer(100)
        with patch.object(m, "download_native", return_value=self.download(9)) as download, \
                patch.object(m, "load_native_layer", return_value=(partial, {})):
            result = m.load_native_layers(WHEN)
        self.assertEqual(download.call_count, 2)
        self.assertIs(result[self.key][0], partial)

    def test_failed_retry_keeps_the_first_successful_partial(self):
        partial = layer(100)
        with patch.object(m, "download_native", side_effect=[self.download(9), self.download(0)]), \
                patch.object(m, "load_native_layer", return_value=(partial, {})) as decode:
            result = m.load_native_layers(WHEN)
        self.assertEqual(decode.call_count, 1)
        self.assertIs(result[self.key][0], partial)

    def test_decode_failure_on_retry_keeps_the_first_successful_partial(self):
        partial = layer(100)
        with patch.object(m, "download_native", side_effect=[self.download(9), self.download(10)]), \
                patch.object(m, "load_native_layer", side_effect=[(partial, {}), RuntimeError("offline decode failure")]):
            result = m.load_native_layers(WHEN)
        self.assertIs(result[self.key][0], partial)

    def test_complete_channel_is_not_retried(self):
        with patch.object(m, "download_native", return_value=self.download(10)) as download, \
                patch.object(m, "load_native_layer", return_value=(layer(100), {})):
            m.load_native_layers(WHEN)
        self.assertEqual(download.call_count, 1)


class WcsDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(m.time, "monotonic", side_effect=lambda: self.clock))
        self.sleep = self.patches.enter_context(patch.object(m.time, "sleep", side_effect=self.advance))

    def advance(self, seconds):
        self.clock += seconds

    def get(self, deadline):
        return m.wcs_get("offline", WHEN, (0, 1), (0, 1), deadline=deadline)

    def test_timeout_is_capped_and_success_does_not_sleep(self):
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(b"II*\0offline")) as request:
            self.assertEqual(self.get(1.), b"II*\0offline")
        self.assertEqual(request.call_args.kwargs["timeout"], 1.)
        self.sleep.assert_not_called()

    def test_retry_sleep_stops_at_deadline(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=TimeoutError("offline")) as request:
            with self.assertRaises(RuntimeError):
                self.get(1.)
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_called_once_with(1.)
        self.assertEqual(self.clock, 1.)

    def test_expired_deadline_starts_no_request(self):
        with patch.object(m.urllib.request, "urlopen") as request:
            with self.assertRaises(RuntimeError):
                self.get(0.)
        request.assert_not_called()
        self.sleep.assert_not_called()

    def test_attempt_consuming_budget_does_not_sleep_or_retry(self):
        def fail(*args, **kwargs):
            self.advance(1.)
            raise TimeoutError("offline")

        with patch.object(m.urllib.request, "urlopen", side_effect=fail) as request:
            with self.assertRaises(RuntimeError):
                self.get(1.)
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_no_deadline_keeps_normal_timeout(self):
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(b"II*\0offline")) as request:
            self.get(None)
        self.assertEqual(request.call_args.kwargs["timeout"], 120.)

    def test_retry_uses_the_budget_remaining_after_request_and_sleep(self):
        def first(*args, **kwargs):
            self.advance(119.)
            raise TimeoutError("offline")

        outcomes = [first, lambda *args, **kwargs: io.BytesIO(b"II*\0offline")]

        def request(*args, **kwargs):
            return outcomes.pop(0)(*args, **kwargs)

        with patch.object(m.urllib.request, "urlopen", side_effect=request) as get:
            self.get(125.)
        self.assertEqual([call.kwargs["timeout"] for call in get.call_args_list], [120., 4.])
        self.sleep.assert_called_once_with(2.)


class EmptyFrameTests(unittest.TestCase):
    def test_empty_frame_is_rejected_before_upload(self):
        store = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "acquire_sources", return_value=[]), \
                patch.object(m, "build_frame_pack", return_value={"tiles": 0, "coverage_fraction": 0., "pack_bytes": 10932}):
            with self.assertRaisesRegex(RuntimeError, "Refusing to publish empty satellite frame"):
                publisher.build_frame(store, WHEN, "offline", Path(directory), 50, 1)
        store.upload_pack.assert_not_called()
        store.publish_manifest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
