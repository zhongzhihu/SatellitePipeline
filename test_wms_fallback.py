"""Offline regression checks for EUMETView's missing WCS imagery coverages."""

import contextlib
import datetime as dt
import io
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import numpy as np
from PIL import Image

import mosaic_v2 as m


WHEN = dt.datetime(2026, 10, 13, 22, 30, tzinfo=m.UTC)
COVERAGE = "mtg_fd__ir105_hrfi"
LAYER = "mtg_fd:ir105_hrfi"
BOUNDS = {LAYER: (WHEN - dt.timedelta(days=1), WHEN + dt.timedelta(minutes=30))}


def png(array, **kwargs):
    output = io.BytesIO()
    Image.fromarray(array).save(output, "PNG", **kwargs)
    return io.BytesIO(output.getvalue())


def http_error(body, code=404):
    return urllib.error.HTTPError("https://offline", code, "offline", {}, io.BytesIO(body))


class WcsTransportTests(unittest.TestCase):
    def setUp(self):
        self.cache = patch.object(m, "_WMS_FALLBACKS", set())
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_missing_coverage_switches_to_wms_and_skips_subsequent_wcs_requests(self):
        error = http_error(b'<ExceptionReport><Exception exceptionCode="NoSuchCoverage"/></ExceptionReport>')
        with patch.object(m.time, "monotonic", return_value=0.), \
                patch.object(m.urllib.request, "urlopen", side_effect=error) as request, \
                patch.object(m, "wms_get", return_value=b"fallback") as fallback:
            for _ in range(2):
                self.assertEqual(m.wcs_get(COVERAGE, WHEN, (45, 46), (7, 8), .5, 123), b"fallback")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(fallback.call_args.args, (COVERAGE, WHEN, (45, 46), (7, 8), .5, 123))

    def test_missing_timestamp_keeps_existing_scan_fallback(self):
        error = http_error(b'<ExceptionReport><Exception exceptionCode="NoApplicableCode"/></ExceptionReport>', 500)
        with patch.object(m.urllib.request, "urlopen", side_effect=error), patch.object(m, "wms_get") as fallback:
            with self.assertRaises(m.WcsMissing):
                m.wcs_get(COVERAGE, WHEN, (45, 46), (7, 8))
        fallback.assert_not_called()

    def test_working_wcs_keeps_original_bytes_and_does_not_use_wms(self):
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(b"II*\0original")), \
                patch.object(m, "wms_get") as fallback:
            self.assertEqual(m.wcs_get(COVERAGE, WHEN, (45, 46), (7, 8)), b"II*\0original")
        fallback.assert_not_called()


class WmsMapTests(unittest.TestCase):
    def setUp(self):
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(m, "wms_time_bounds", return_value=BOUNDS))
        self.patches.enter_context(patch.object(m.time, "sleep"))

    def get(self, **kwargs):
        return m.wms_get(COVERAGE, WHEN, (45, 45.02), (7, 7.02), **kwargs)

    def test_raw_gray_alpha_geometry_and_request_parameters(self):
        array = np.array([[[100, 100, 100, 255], [200, 200, 200, 0]],
                          [[150, 150, 150, 128], [0, 0, 0, 255]]], np.uint8)
        with patch.object(m.urllib.request, "urlopen", return_value=png(array)) as request:
            body = self.get()
        gray, lon0, lat0, dx, dy = m.decode_geotiff(body)
        np.testing.assert_array_equal(gray, [[100, 0], [0, 0]])
        self.assertEqual((lon0, lat0), (7, 45.02))
        self.assertAlmostEqual(dx, .01)
        self.assertAlmostEqual(dy, .01)
        with Image.open(io.BytesIO(body)) as image:
            self.assertEqual(image.tag_v2[42113], "0.0")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0].full_url).query)
        self.assertEqual(query["styles"], ["raster"])
        self.assertEqual(query["layers"], [LAYER])
        self.assertEqual(query["crs"], ["EPSG:4326"])
        self.assertEqual(query["bbox"], ["45.000000000,7.000000000,45.020000000,7.020000000"])
        self.assertEqual(query["time"], ["2026-10-13T22:30:00.000Z/2026-10-13T22:30:00.000Z"])

    def test_visible_scaling_matches_infrared_grid(self):
        bounds = {"mtg_fd:vis06_hrfi": BOUNDS[LAYER]}
        with patch.object(m, "wms_time_bounds", return_value=bounds), \
                patch.object(m.urllib.request, "urlopen", return_value=png(np.full((2, 2), 80, np.uint8))) as request:
            m.wms_get("mtg_fd__vis06_hrfi", WHEN, (45, 45.02), (7, 7.02), .7125)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0].full_url).query)
        self.assertEqual(query["width"], ["2"])
        self.assertEqual(query["height"], ["2"])

    def test_large_map_pieces_share_grid_without_gaps_or_flipped_axes(self):
        expected = np.arange(1, 17, dtype=np.uint8).reshape(4, 4)

        def request(req, **kwargs):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            south, west, north, east = map(float, q["bbox"][0].split(","))
            row, col = round((45.04 - north) / .01), round((west - 7) / .01)
            h, w = int(q["height"][0]), int(q["width"][0])
            self.assertLessEqual(h, 3)
            self.assertLessEqual(w, 3)
            return png(expected[row:row + h, col:col + w])

        with patch.object(m, "WMS_MAX_SIZE", 3), patch.object(m.urllib.request, "urlopen", side_effect=request) as get:
            body = m.wms_get(COVERAGE, WHEN, (45, 45.04), (7, 7.04))
        self.assertEqual(get.call_count, 4)
        np.testing.assert_array_equal(m.decode_geotiff(body)[0], expected)

    def test_palette_transparency_is_nodata(self):
        image = Image.fromarray(np.array([[1, 2], [2, 1]], np.uint8)).convert("P")
        palette = [0] * 768
        palette[3:6], palette[6:9] = [90, 90, 90], [200, 200, 200]
        image.putpalette(palette)
        output = io.BytesIO()
        image.save(output, "PNG", transparency=2)
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(output.getvalue())):
            gray = m.decode_geotiff(self.get())[0]
        np.testing.assert_array_equal(gray, [[90, 0], [0, 90]])

    def test_unavailable_time_does_not_download_a_nearest_scan(self):
        with patch.object(m, "wms_time_bounds", return_value={LAYER: (WHEN - dt.timedelta(days=1), WHEN - dt.timedelta(minutes=10))}), \
                patch.object(m.urllib.request, "urlopen") as request:
            with self.assertRaises(m.WcsMissing):
                self.get()
        request.assert_not_called()

    def test_missing_layer_is_unavailable(self):
        with patch.object(m, "wms_time_bounds", return_value={}), patch.object(m.urllib.request, "urlopen") as request:
            with self.assertRaises(m.WcsMissing):
                self.get()
        request.assert_not_called()

    def test_failed_piece_rejects_entire_map(self):
        outcomes = [png(np.full((2, 2), 80, np.uint8))] + [TimeoutError("missing piece")] * 8
        with patch.object(m, "WMS_MAX_SIZE", 2), patch.object(m.urllib.request, "urlopen", side_effect=outcomes):
            with self.assertRaisesRegex(RuntimeError, "missing piece"):
                m.wms_get(COVERAGE, WHEN, (45, 45.04), (7, 7.04))

    def test_deadline_bounds_request_and_stops_retry(self):
        clock = [0.]

        def request(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 1.)
            clock[0] = 1.
            raise TimeoutError("budget used")

        with patch.object(m.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(m.urllib.request, "urlopen", side_effect=request) as get:
            with self.assertRaisesRegex(RuntimeError, "budget used"):
                self.get(deadline=1.)
        self.assertEqual(get.call_count, 1)
        m.time.sleep.assert_not_called()

    def test_wrong_size_and_error_payload_are_rejected(self):
        for body in (b"<ServiceExceptionReport/>", png(np.full((1, 1), 80, np.uint8)).getvalue()):
            with self.subTest(body=body[:8]), patch.object(m.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(body)):
                with self.assertRaises(RuntimeError):
                    self.get()


class WmsCatalogueTests(unittest.TestCase):
    def test_bounds_are_parsed_cached_and_refreshed(self):
        xml = b'''<WMS_Capabilities xmlns="http://www.opengis.net/wms"><Capability><Layer>
          <Layer><Name>mtg_fd:ir105_hrfi</Name><Dimension name="time">
          2026-10-13T00:00:00.000Z/2026-10-13T23:00:00.000Z/PT10M
          </Dimension></Layer></Layer></Capability></WMS_Capabilities>'''
        with patch.object(m, "_WMS_TIMES", {}), patch.object(m, "_WMS_TIMES_AT", -float("inf")), \
                patch.object(m.time, "monotonic", return_value=0.) as clock, \
                patch.object(m.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(xml)) as request:
            for _ in range(2):
                self.assertEqual(m.wms_time_bounds(1.)[LAYER][1], WHEN + dt.timedelta(minutes=30))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["timeout"], 1.)
            clock.return_value = 61.
            m.wms_time_bounds(62.)
            self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
