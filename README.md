# Satellite observation pipeline

Builds packed infrared and visible observation tiles from public NOAA and EUMETSAT feeds, then uploads each frame directly to R2 through its S3 API. A Cloudflare Worker serves the manifest and tiles to clients.

The scheduled GitHub Actions workflow runs every ten minutes and can also be started manually. A cold start or manual restart searches up to three hours back for the newest ten-minute slot with ready GOES and Himawari inputs, then publishes that frame alone. Scheduled runs continue from the last published timestamp by exactly ten minutes, so a restart does not build an old backlog before reaching current data. The manifest grows from one to six contiguous frames as scheduled runs succeed. A shared Actions concurrency group serializes workflow runs, and the publisher also preserves ordering if frame builds overlap: `manifest.json` is replaced with a conditional write against the version the run read, and a timeline older than the published one is rejected unless a run backfills from an explicit `SATELLITE_START_UTC`.

After each manifest update the publisher keeps the packs referenced by the current and previous manifests. It deletes other packs once they are older than three hours, so clients with a briefly cached previous manifest can continue fetching its tiles.

## Dawn rendering check

`python3 check_dawn.py` downloads small MTG IR/VIS crops for the six frames from 06:50 through 07:40 UTC on 3 October 2026. It caches inputs and writes before/after images and opacity-change metrics to `results/dawn/`; it does not publish tiles. This optional diagnostic needs Matplotlib in addition to the pipeline's NumPy/Pillow dependencies.

"Before" reproduces the settings behind the 4 October 2026 screenshots, where scrubbing 10 minutes visibly brightened clouds west of Europe. "After" uses the current publisher and app code. Low-sun visible imagery stays steady between frames because of four changes:

- MTG gray is decoded through its measured gamma-like transfer to the FES scale (see `MTG_VIS_GRAY_LUT`).
- Each source's reflectance is normalised by the sun at that source's actual slot, including fallbacks, plus the per-pixel scan offset (`scan_offset_minutes`).
- The normalisation floor is cos(sza) 0.08.
- Low-sun haze is subtracted without re-stretching.

In the app, the visible weight rises linearly in solar time over 2.5 hours after the sun crosses cos(sza) 0.05, or until local noon when that is sooner, and falls symmetrically in the afternoon. Red-band MTG imagery shows low cloud that IR misses. A ramp in cos(sza) reveals it mostly in the first hour after sunrise and sweeps a brightening band west across the Atlantic between 10-minute frames. A ramp to noon kept the per-frame change smallest, but on 6 October 2026 it left a stratocumulus deck over the Netherlands at about a third of its opacity at 07:40 UTC, while forecasts showed overcast. With 2.5 hours, mean brightening on the 3 October crops is about 0.6–1.3 gray levels per frame, against 2.5–2.9 for "Before". Winter high latitudes still reach the full daytime look at noon.

EUMETView supplies 8-bit grayscale imagery without a published calibration. Treating FES gray/255 as reflectance is an approximation. The previews isolate MTG and omit satellite fallbacks, the basemap and AVIF compression.

## GitHub settings

Set repository secrets `R2_ACCOUNT_ID`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`, the last two from an R2 API token with Object Read & Write scoped to that bucket. Set variable `SATELLITE_STORAGE_PREFIX` only if the Worker uses a prefix; the two must match. The workflow keeps bootstrap lookback, image quality, worker count, and input wait settings in its environment.

Run `python3 -m unittest -v test_r2_store` to check manifest publication and pack retention.

Hosted runners start with an empty filesystem. Downloaded source data is cached only during a workflow run, while pip packages use the Actions cache.

MTG is downloaded in 35° chunks. If any chunk fails, the entire channel is rejected and both IR and VIS are retried together at an earlier scan, up to 30 minutes back. If no complete scan is available within the download budget, the other satellites supply coverage. Do not publish partial MTG canvases: filling individual holes from MSG produces rectangular brightness seams between differently calibrated products. Run `python3 -m unittest -v test_wcs_completeness` to check this behavior.

The MTG visible WCS gray index is mapped to the Meteosat 0° (FES) gray index before reflectance encoding. EUMETView's MTG product is gamma-encoded and FES is linear. The table in `mosaic_v2.py` holds medians of collocated scans from 3–4 October 2026, over Europe, the Atlantic and Africa, and is close to FES ≈ 0.105 × MTG^1.406. This keeps FES fallback frames and MTG frames at matching brightness, and keeps MTG dawn clouds from brightening faster than the sun. It is a display calibration, not a physical radiance calibration.

## Worker

`worker/src/index.js` serves the signed client manifest and tiles from R2; it has no publishing routes. Its account-specific Wrangler configuration is kept locally in the ignored `worker/wrangler.jsonc`; configure the R2 binding and custom domain there when deploying the Worker. Configure these Worker secrets: `SATELLITE_TILE_SIGNING_SECRET` and `SATELLITE_CLIENT_AUTH_SECRET`. Set `SATELLITE_CLIENT_AUTH_HEADER` to the header sent by your client, and set `SATELLITE_CLIENT_AUTH_SECRET` to the matching client token. A value bundled with a client is an access gate, not a confidential secret. The tile-signing secret remains server-side. `SATELLITE_STORAGE_PREFIX` selects an optional object prefix shared by the Worker and publisher; unset or empty publishes `manifest.json` and `packs/<frame>-<revision>.pack` directly at the bucket root. A nonempty value is used literally, without an appended version folder. The `/v2/...` API routes and manifest schema remain versioned independently of the R2 layout.
