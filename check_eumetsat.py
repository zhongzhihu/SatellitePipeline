#!/usr/bin/env python3
"""Check authenticated EUMETSAT downloads and decoding without publishing."""
import concurrent.futures
import datetime as dt
import sys
import time

import numpy as np

import eumetsat_store as eumetsat
import mosaic_v2 as mosaic


def main() -> int:
    credentials = eumetsat.credentials_from_environment()
    if not credentials:
        print("Both EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET are required.", flush=True)
        return 1
    mosaic.configure_fci_decompression()
    now = dt.datetime.now(dt.timezone.utc)
    when = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    print(f"Checking Data Store scans at or before {when.isoformat()}", flush=True)
    client = eumetsat.DataStore(credentials)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        downloads = {name: pool.submit(mosaic.download_store_source, name, when, client)
                     for name in ("MTG", "IODC")}
        results = {name: future.result() for name, future in downloads.items()}
    ok = True
    for name in ("MTG", "IODC"):
        download = results[name]
        if download is None:
            print(f"{name}: no usable authenticated product", flush=True)
            ok = False
            continue
        try:
            started = time.perf_counter()
            ir, vis, info = mosaic.decode_store_source(name, when, download)
            print(f"{name}: scan={info['timestamp']}, age={info['fallback_minutes']} minutes, "
                  f"IR shape={ir.codes.shape}, visible={'available' if vis is not None else 'unavailable'}, "
                  f"fetch={info['fetch_seconds']}s, decode={time.perf_counter() - started:.1f}s", flush=True)
            if name == "MTG":
                lon = np.array([0., 20., 0., 20.])
                lat = np.array([45., 45., 55., 55.])
                row, col = ir.fractional_index(lon, lat)
                _, valid = mosaic.bilinear_sample(ir.codes, row, col)
                if not (valid > .5).all():
                    raise RuntimeError("European infrared coverage check failed")
        except Exception as exc:
            print(f"{name}: decode/coverage check failed: {exc}", flush=True)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
