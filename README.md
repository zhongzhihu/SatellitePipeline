# Satellite observation pipeline

Builds packed infrared and visible observation tiles from NOAA and EUMETSAT feeds, then uploads each frame directly to R2 through its S3 API. A Cloudflare Worker serves the manifest and tiles to clients.

The GitHub Actions workflow maintains a ten-minute cadence through a continuation job that dispatches the next run, with an hourly cron as a recovery trigger. It can also be started manually. A cold start or manual restart searches up to three hours back for the newest ten-minute slot with ready GOES and Himawari inputs, then publishes that frame alone. Normal runs advance the last published timestamp by ten minutes. When the published timeline is at least 40 minutes behind the current time and the newest ready input is more than one slot ahead, the publisher reseeds at that newest slot. The manifest grows from one to six contiguous frames as runs succeed. A shared Actions concurrency group serializes publishing runs, including the continuation job; the non-publishing EUMETSAT diagnostic has a separate group. The publisher replaces `manifest.json` with a conditional write against the version read immediately before publication and rejects an older timeline unless `SATELLITE_START_UTC` explicitly allows rewinding.

After each manifest update the publisher keeps the packs referenced by the current and previous manifests. It deletes other packs once they are older than three hours, so clients with a briefly cached previous manifest can continue fetching its tiles.

## Dawn rendering check

`python3 check_dawn.py` downloads small EUMETView MTG IR/VIS crops for the six frames from 06:50 through 07:40 UTC on 3 October 2026. It caches inputs and writes before/after images and opacity-change metrics to `results/dawn/`; it does not publish tiles. This optional diagnostic needs Matplotlib in addition to the pipeline's NumPy/Pillow dependencies and does not test the Data Store path.

"Before" reproduces the settings behind the 4 October 2026 screenshots, where scrubbing 10 minutes visibly brightened clouds west of Europe. "After" uses the current publisher and app code. Low-sun visible imagery stays steady between frames because of four changes:

- MTG gray is decoded through its measured gamma-like transfer to the FES scale (see `MTG_VIS_GRAY_LUT`).
- Each source's reflectance is normalised by the sun at that source's actual slot, including fallbacks, plus the per-pixel scan offset (`scan_offset_minutes`).
- The normalisation floor is cos(sza) 0.08.
- Low-sun haze is subtracted without re-stretching.

In the app, the visible weight rises linearly in solar time over 2.5 hours after the sun crosses cos(sza) 0.05, or until local noon when that is sooner, and falls symmetrically in the afternoon. Red-band MTG imagery shows low cloud that IR misses. A ramp in cos(sza) reveals it mostly in the first hour after sunrise and sweeps a brightening band west across the Atlantic between 10-minute frames. A ramp to noon kept the per-frame change smallest, but on 6 October 2026 it left a stratocumulus deck over the Netherlands at about a third of its opacity at 07:40 UTC, while forecasts showed overcast. With 2.5 hours, mean brightening on the 3 October crops is about 0.6–1.3 gray levels per frame, against 2.5–2.9 for "Before". Winter high latitudes still reach the full daytime look at noon.

EUMETView supplies 8-bit grayscale imagery without a published calibration. Treating FES gray/255 as reflectance is an approximation. The previews isolate MTG and omit satellite fallbacks, the basemap and AVIF compression.

## GitHub settings

Set repository secrets `R2_ACCOUNT_ID`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`, the last two from an R2 API token with Object Read & Write scoped to that bucket. Set variable `SATELLITE_STORAGE_PREFIX` only if the Worker uses a prefix; the two must match. The workflow keeps bootstrap lookback, image quality, worker count, and input wait settings in its environment.

For the EUMETSAT Data Store, create a EUMETSAT account, obtain an active near-real-time Meteosat licence for the intended use, and open [API Key Management](https://api.eumetsat.int/api-key/). In GitHub, open the repository's **Settings → Secrets and variables → Actions → New repository secret** and add:

- `EUMETSAT_CONSUMER_KEY`: the Consumer Key from EUMETSAT.
- `EUMETSAT_CONSUMER_SECRET`: the Consumer Secret from EUMETSAT.

Use the consumer credentials, not the temporary access token, and never commit them or put them in workflow inputs. Once the change is present on the branch being run, use **Actions → Publish satellite frames → Run workflow**, select that branch, and enable **Check EUMETSAT downloads without publishing or scheduling a continuation**. This diagnostic downloads and decodes both native products, checks European infrared coverage, and reports the chosen scan times. It does not write to R2. An ordinary manual run leaves that option off and publishes normally. A newly granted licence may take up to an hour after logging in again to reach the Data Store; HTTP 401/403 in the check points to credentials or licensing.

With both secrets set, the publisher prefers the Data Store's MTG FCI HRFI full disk (`EO:EUM:DAT:0665`) and Indian Ocean SEVIRI (`EO:EUM:DAT:MSG:HRSEVIRI-IODC`). It uses the same authenticated REST endpoints as EUMDAC with bounded request timeouts, verifies the product archive against the published MD5 when present and the ZIP checksums, and requires all 40 MTG image chunks. Satpy decodes calibrated infrared brightness temperature and visible reflectance; MTG's 500 m visible grid is sampled at 1 km to reduce memory and match the infrared grid. Each source uses its actual scan time for sun correction. The search never chooses a future scan and allows MTG up to 30 minutes old and IODC up to 75 minutes old. Data Store availability and IODC cadence may result in older source scans than the frame timestamp; the manifest's `source_availability` records the timestamp, age, and backend.

If credentials are absent or a Data Store source fails, EUMETView remains the fallback. Each EUMETView source gets its own deadline and runs independently, so a slow FES request cannot consume MTG's time budget. FES is attempted as a fallback when Data Store MTG fails. The existing European infrared publication guard remains in place. `python3 check_eumetsat.py` runs the same diagnostic locally when the two credentials are supplied through environment variables. Run `python3 -m unittest -v test_eumetsat_store` for the offline acquisition regressions.

Run `python3 -m unittest -v test_r2_store` to check manifest publication and pack retention.

Run `python3 -m unittest discover -v` and `node --test worker/test/storage.test.mjs` to run the offline regression suites. Completely empty frames are rejected before upload, preserving the published timeline.

Hosted runners start with an empty filesystem. Downloaded source data is cached only during a workflow run, while pip packages use the Actions cache.

Failed native channels and Himawari channels with fewer than ten downloaded segments are retried once. If the retry still cannot complete a Himawari channel, a successfully decoded partial layer is retained to preserve usable coverage. Source weights are selected separately for IR and VIS, so a missing channel does not suppress another satellite's channel. WCS gray zero remains nodata through IR and visible calibration.

In the EUMETView fallback, MTG is downloaded in 35° chunks. If any chunk fails, the entire channel is rejected and both IR and VIS are retried together at an earlier scan, up to 30 minutes back. If no complete scan is available within the download budget, the other satellites supply coverage. Do not publish partial MTG canvases: filling individual holes from MSG produces rectangular brightness seams between differently calibrated products. Run `python3 -m unittest -v test_wcs_completeness` to check this behavior.

For EUMETView fallback only, the MTG visible WCS gray index is mapped to the Meteosat 0° (FES) gray index before reflectance encoding. EUMETView's MTG product is gamma-encoded and FES is linear. The table in `mosaic_v2.py` holds medians of collocated scans from 3–4 October 2026, over Europe, the Atlantic and Africa, and is close to FES ≈ 0.105 × MTG^1.406. This keeps FES fallback frames and MTG frames at matching brightness, and keeps MTG dawn clouds from brightening faster than the sun. It is a display calibration, not a physical radiance calibration; the native Data Store path does not use it.

If EUMETView removes an imagery product from its WCS catalogue (`NoSuchCoverage`), acquisition switches that product to WMS GetMap using the plain `raster` style, lossless PNG, and approximately the same native pixel spacing. Large maps are split on a shared grid to bound server rendering memory; transparency stays nodata and the existing gray calibration tables still apply. The fallback checks advertised time bounds and requests an exact time interval, retaining complete IR when visible imagery stops updating overnight. It shares the existing download deadline and whole-coverage rejection policy. Run `python3 -m unittest -v test_wms_fallback test_wcs_completeness` to check the fallback.

Before upload, the publisher requires valid Meteosat IR samples at 45°/55° N and 0°/20° E. If European IR coverage is missing, publication fails and the previous timeline remains available instead of replacing it with a frame missing Europe. Visible imagery is optional at night.

## Worker

`worker/src/index.js` serves the signed client manifest and tiles from R2; it has no publishing routes. Its account-specific Wrangler configuration is kept locally in the ignored `worker/wrangler.jsonc`; configure the R2 binding and custom domain there when deploying the Worker. Configure these Worker secrets: `SATELLITE_TILE_SIGNING_SECRET` and `SATELLITE_CLIENT_AUTH_SECRET`. Set `SATELLITE_CLIENT_AUTH_HEADER` to the header sent by your client, and set `SATELLITE_CLIENT_AUTH_SECRET` to the matching client token. A value bundled with a client is an access gate, not a confidential secret. The tile-signing secret remains server-side. `SATELLITE_STORAGE_PREFIX` selects an optional object prefix shared by the Worker and publisher; unset or empty publishes `manifest.json` and `packs/<frame>-<revision>.pack` directly at the bucket root. A nonempty value is used literally, without an appended version folder. The `/v2/...` API routes and manifest schema remain versioned independently of the R2 layout.
