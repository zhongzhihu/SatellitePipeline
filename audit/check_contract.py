#!/usr/bin/env python3
"""Offline checks of the app's projection, AVIF layout, and pack contract.

Run from the repository: python audit/check_contract.py
Uses temporary local packs; does not download data or publish to R2.
Behavior regressions live in test_pipeline.py.
"""

import io
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer

import mosaic_v2 as m


def geometry_agrees_with_proj():
    rng = np.random.default_rng(7)
    lon, lat = rng.uniform(-180, 180, 10_000), rng.uniform(-75, 75, 10_000)
    for sweep in ("x", "y"):
        crs = CRS.from_proj4(f"+proj=geos +lon_0=-75 +h=35786023 +a=6378137 +b=6356752.31414 +sweep={sweep}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            x, y = m.Geos.from_crs(crs).forward(lon, lat)
        expected_x, expected_y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
        visible = np.isfinite(expected_x) & np.isfinite(expected_y)
        assert np.array_equal(np.isfinite(x), visible)
        np.testing.assert_allclose(x[visible], expected_x[visible], atol=1e-5, rtol=0)
        np.testing.assert_allclose(y[visible], expected_y[visible], atol=1e-5, rtol=0)
    print("CONTROL: geostationary projection matches PROJ for 10,000 points in each sweep mode")


def encoding_and_pack_contract():
    ir = np.full((1024, 1024), 100, np.uint8)
    vis = np.full((1024, 1024), 200, np.uint8)
    tile = m.encode_tile(ir, vis, 50)
    with Image.open(io.BytesIO(tile)) as image:
        assert image.format == "AVIF" and image.size == (1024, 2048)
        decoded = np.asarray(image.convert("L"))
        assert abs(float(decoded[:1024].mean()) - 100) <= 2
        assert abs(float(decoded[1024:].mean()) - 200) <= 2
    addresses = [(z, x, y) for z in range(1, 6) for y in range(1 << z) for x in range(1 << z)]
    assert [m.tile_index(*address) for address in addresses] == list(range(1364))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "offline.pack"
        tiles = {(1, 0, 0): tile, (5, 31, 31): tile}
        size = m.write_pack(path, tiles)
        data = path.read_bytes()
        assert len(data) == size
        assert m.PACK_HEADER.unpack_from(data) == (b"WXSP", 3, 1, 5, 1364)
        for address in addresses:
            offset, length = m.INDEX_ENTRY.unpack_from(data, 20 + m.tile_index(*address) * 8)
            if address in tiles:
                assert offset >= 10932 and data[offset:offset + length] == tiles[address]
            else:
                assert (offset, length) == (0, 0)
    print("CONTROL: AVIF layout, WXSP v3 header, all 1,364 index addresses, and tile ranges agree")


if __name__ == "__main__":
    geometry_agrees_with_proj()
    encoding_and_pack_contract()
