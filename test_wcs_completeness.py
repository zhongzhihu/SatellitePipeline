"""Regression checks for rectangular seams caused by partial MTG downloads."""
import datetime as dt
import unittest
from unittest.mock import patch

import numpy as np

import mosaic_v2 as m


class WcsCompletenessTests(unittest.TestCase):
    when = dt.datetime(2026, 10, 3, 10, 20, tzinfo=m.UTC)
    source = ("ir", "vis", 10, (-2.0, 2.0), (-2.0, 2.0), 2.0, None)

    def get_chunk(self, coverage, when, lat, lon, scale, deadline):
        value = 10 + int(lat[0] + 2) * 2 + int(lon[0] + 2)
        return np.full((2, 2), value, np.uint8), lon[0], lat[1], 1.0, 1.0

    def test_complete_chunks_keep_correct_geographic_placement(self):
        with patch.dict(m.WCS_SOURCES, MTG=self.source), \
                patch.object(m, "wcs_get", side_effect=self.get_chunk), \
                patch.object(m, "decode_geotiff", side_effect=lambda result: result):
            layer = m.fetch_wcs_coverage("MTG", "ir", self.when, np.arange(256, dtype=np.uint8), None, 999)
        expected = np.array([[14, 14, 16, 16], [14, 14, 16, 16],
                             [10, 10, 12, 12], [10, 10, 12, 12]], dtype=np.uint8)
        np.testing.assert_array_equal(layer.codes, expected)

    def test_failed_europe_chunk_rejects_whole_coverage(self):
        def get(coverage, when, lat, lon, scale, deadline):
            if lat == (0.0, 2.0) and lon == (-2.0, 0.0):
                raise m.WcsMissing("Europe chunk not yet available")
            return self.get_chunk(coverage, when, lat, lon, scale, deadline)

        with patch.dict(m.WCS_SOURCES, MTG=self.source), \
                patch.object(m, "wcs_get", side_effect=get), \
                patch.object(m, "decode_geotiff", side_effect=lambda result: result):
            with self.assertRaisesRegex(m.WcsIncomplete, "1/4 chunks failed"):
                m.fetch_wcs_coverage("MTG", "ir", self.when, np.arange(256, dtype=np.uint8), None, 999)

    def test_transient_chunk_error_also_rejects_coverage(self):
        def get(coverage, when, lat, lon, scale, deadline):
            if lat == (0.0, 2.0) and lon == (-2.0, 0.0):
                raise RuntimeError("download deadline exceeded")
            return self.get_chunk(coverage, when, lat, lon, scale, deadline)

        with patch.dict(m.WCS_SOURCES, MTG=self.source), \
                patch.object(m, "wcs_get", side_effect=get), \
                patch.object(m, "decode_geotiff", side_effect=lambda result: result):
            with self.assertRaises(m.WcsIncomplete):
                m.fetch_wcs_coverage("MTG", "ir", self.when, np.arange(256, dtype=np.uint8), None, 999)

    def test_partial_visible_scan_retries_both_channels_at_same_time(self):
        earlier = self.when - dt.timedelta(minutes=10)
        calls = []
        layer = m.NativeLayer(np.full((2, 2), 100, np.uint8), None, -2, 2, 1, 1)

        def fetch(name, coverage, when, lut, scale, deadline):
            calls.append((coverage, when))
            if when == self.when and coverage == "vis":
                raise m.WcsIncomplete("missing Europe VIS chunk")
            return layer

        with patch.dict(m.WCS_SOURCES, MTG=self.source), \
                patch.object(m, "fetch_wcs_coverage", side_effect=fetch):
            ir, vis, info = m.fetch_wcs_source("MTG", self.when, 999)
        self.assertIs(ir, layer)
        self.assertIs(vis, layer)
        self.assertEqual(info["timestamp"], earlier.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(info["fallback_minutes"], 10)
        self.assertEqual(set(calls), {("ir", self.when), ("vis", self.when), ("ir", earlier), ("vis", earlier)})

    def test_no_complete_scan_disables_source_instead_of_publishing_holes(self):
        with patch.dict(m.WCS_SOURCES, MTG=self.source), \
                patch.object(m, "fetch_wcs_coverage", side_effect=m.WcsIncomplete("partial")):
            ir, vis, info = m.fetch_wcs_source("MTG", self.when, 999)
        self.assertIsNone(ir)
        self.assertIsNone(vis)
        self.assertFalse(info["available"])


if __name__ == "__main__":
    unittest.main()
