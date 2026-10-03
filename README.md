# Satellite observation pipeline

Builds packed infrared and visible observation tiles from public NOAA and EUMETSAT feeds, then publishes each frame through a Cloudflare Worker to R2.

The scheduled GitHub Actions workflow runs every ten minutes and can also be started manually. A cold start or manual restart searches up to three hours back for the newest ten-minute slot with ready GOES and Himawari inputs, then publishes that frame alone. Scheduled runs continue from the last published timestamp by exactly ten minutes, so a restart does not build an old backlog before reaching current data. The manifest grows from one to six contiguous frames as scheduled runs succeed. A shared Actions concurrency group serializes workflow runs, and the publisher also preserves ordering if frame builds overlap.

Each manifest update retains the packs referenced by the current and previous manifests. Other packs are eligible for cleanup after the three-hour retention window, so clients with a briefly cached previous manifest can continue fetching its tiles.

## Dawn rendering check

`python3 check_dawn.py` downloads small MTG IR/VIS crops for the six frames from 06:50 through 07:40 UTC on 3 October 2026. It caches inputs and writes before/after images and opacity-change metrics to `results/dawn/`; it does not publish tiles. This optional diagnostic needs Matplotlib in addition to the pipeline's NumPy/Pillow dependencies.

The comparison preserves the old app blend as a baseline and tests the wider 0.10–0.30 solar-cosine transition in VistaWeather's `ForecastMapSatelliteColouriser`. The later transition retains infrared cloud support while visible illumination stabilizes. Lowering the publisher's normalization floor from 0.2 to 0.1 added little improvement in this crop, so the production tile processing remains unchanged. EUMETView supplies 8-bit grayscale imagery without a sufficient published calibration mapping in its coverage metadata; treating gray/255 as reflectance remains an approximation, not a verified physical calibration. These cropped previews isolate MTG and omit satellite fallbacks, the basemap, and AVIF compression.

## GitHub settings

Set repository variable `SATELLITE_PUBLISHER_URL` to the Worker base URL and secret `SATELLITE_PUBLISH_TOKEN` to its publisher token. The workflow keeps bootstrap lookback, image quality, worker count, and input wait settings in its environment.

Hosted runners start with an empty filesystem. Downloaded source data is cached only during a workflow run, while pip packages use the Actions cache.

MTG is downloaded in 35° chunks. If any chunk fails, the entire channel is rejected and both IR and VIS are retried together at an earlier scan, up to 30 minutes back. If no complete scan is available within the download budget, the other satellites supply coverage. Do not publish partial MTG canvases: filling individual holes from MSG produces rectangular brightness seams between differently calibrated products. Run `python3 -m unittest -v test_wcs_completeness` to check this behavior.

The MTG visible WCS gray index is aligned to the Meteosat 0° (FES) gray index before reflectance encoding. Matching 3 October 2026 scans over Europe, the Atlantic, and Africa gave an approximate mapping of `FES gray = 0.86 × MTG gray − 15`. This display-level correction reduces the brightness jump when an incomplete MTG scan falls back to FES and the next frame returns to MTG. It does not change the infrared data or claim a physical radiance calibration.

## Worker

`worker/src/index.js` implements the upload, manifest, retention, and tile-serving API. Its account-specific Wrangler configuration is kept locally in the ignored `worker/wrangler.jsonc`; configure the R2 binding and custom domain there when deploying the Worker. Configure these Worker secrets: `SATELLITE_PUBLISHER_TOKEN`, `SATELLITE_TILE_SIGNING_SECRET`, and `SATELLITE_CLIENT_AUTH_SECRET`. Set `SATELLITE_CLIENT_AUTH_HEADER` to the header sent by your client, and set `SATELLITE_CLIENT_AUTH_SECRET` to the matching client token. A value bundled with a client is an access gate, not a confidential secret. The tile-signing secret and publisher token remain server-side. `SATELLITE_STORAGE_PREFIX` selects the object prefix used by the Worker.
