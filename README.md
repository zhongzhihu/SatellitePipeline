# Satellite observation pipeline

Builds packed infrared and visible observation tiles from public NOAA and EUMETSAT feeds, then publishes each frame through a Cloudflare Worker to R2.

The scheduled GitHub Actions workflow runs every ten minutes and can also be started manually. A cold start or manual restart searches up to three hours back for the newest ten-minute slot with ready GOES and Himawari inputs, then publishes that frame alone. Scheduled runs continue from the last published timestamp by exactly ten minutes, so a restart does not build an old backlog before reaching current data. The manifest grows from one to six contiguous frames as scheduled runs succeed. A shared Actions concurrency group serializes workflow runs, and the publisher also preserves ordering if frame builds overlap.

Each manifest update retains the packs referenced by the current and previous manifests. Other packs are eligible for cleanup after the three-hour retention window, so clients with a briefly cached previous manifest can continue fetching its tiles.

## GitHub settings

Set repository variable `SATELLITE_PUBLISHER_URL` to the Worker base URL and secret `SATELLITE_PUBLISH_TOKEN` to its publisher token. The workflow keeps bootstrap lookback, image quality, worker count, and input wait settings in its environment.

Hosted runners start with an empty filesystem. Downloaded source data is cached only during a workflow run, while pip packages use the Actions cache.

## Worker

`worker/src/index.js` implements the upload, manifest, retention, and tile-serving API. Its account-specific Wrangler configuration is kept locally in the ignored `worker/wrangler.jsonc`; configure the R2 binding and `satellite.vistaweather.com` custom domain there when deploying the Worker. Configure these Worker secrets: `SATELLITE_PUBLISHER_TOKEN`, `SATELLITE_TILE_SIGNING_SECRET`, and `SATELLITE_CLIENT_AUTH_SECRET`. The VistaWeather app sends its existing `X-Vista-Weather` header; set `SATELLITE_CLIENT_AUTH_SECRET` to the same value configured as `VistaWeatherWAFSecret` in the app's ignored `Config/Secrets.local.xcconfig`. This app-bundled value is an access gate, not a confidential secret. The tile-signing secret and publisher token remain server-side. `SATELLITE_STORAGE_PREFIX` selects the object prefix used by the Worker.
