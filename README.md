# Satellite observation pipeline

Builds packed infrared and visible observation tiles from public NOAA and EUMETSAT feeds, then publishes each frame through a Cloudflare Worker to R2.

The scheduled GitHub Actions workflow runs every ten minutes and can also be started manually. It processes one new frame per run. On a cold start, the first valid frame is published immediately; subsequent runs extend the manifest with contiguous frames up to the six-frame limit. During bootstrap, consumers should accept a manifest containing between one and six frames. A shared Actions concurrency group serializes workflow runs, and the publisher also preserves ordering if frame builds overlap.

Each manifest update retains the packs referenced by the current and previous manifests. Other packs are eligible for cleanup after the three-hour retention window, so clients with a briefly cached previous manifest can continue fetching its tiles.

## GitHub settings

Set repository variable `SATELLITE_PUBLISHER_URL` to the Worker base URL and secret `SATELLITE_PUBLISH_TOKEN` to its publisher token. The workflow keeps the current frame lag, image quality, worker count, and input wait settings in its environment.

Hosted runners start with an empty filesystem. Downloaded source data is cached only during a workflow run, while pip packages use the Actions cache.

## Worker

`worker/src/index.js` implements the upload, manifest, retention, and tile-serving API. Copy `worker/wrangler.jsonc.example` to `worker/wrangler.jsonc` and set the R2 bucket name before deployment. Configure these Worker secrets: `SATELLITE_PUBLISHER_TOKEN`, `SATELLITE_TILE_SIGNING_SECRET`, and `SATELLITE_CLIENT_AUTH_SECRET`. Set `SATELLITE_CLIENT_AUTH_HEADER` to the header expected from the consuming client and `SATELLITE_STORAGE_PREFIX` to the object prefix used by the Worker.
