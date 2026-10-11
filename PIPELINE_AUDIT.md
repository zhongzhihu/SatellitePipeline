**Satellite pipeline audit — 11 October 2026**

**Implementation update — 12 October 2026:** the agreed small fixes are now in the working tree: channel-specific source weighting, zero-preserving WCS LUTs, one retry for partial Himawari channels while retaining usable coverage, bounded WCS request timeouts/retry sleeps, and rejection of empty frames before upload. The README and encoding-descriptor comment are corrected. `test_pipeline.py` tests the corrected behavior. Pixel-hole fallback, explicit-start handling, concurrent publication, orchestration, and performance refactors remain deferred. The P1 labels below belong to the original audit; the follow-up review lowered their urgency because no production frequency was established. The original known-bad reproduction assertions have been replaced by the regression tests and `audit/check_contract.py` for compatibility checks. The remainder of this report records the original audit of `c8d7ba1`.

Reviewed commit `c8d7ba1`. The pipeline is reasonably small and its overall architecture is appropriate. The main problems are in failure handling and fallback selection, rather than a need for a redesign. Fix the rendering and acquisition defects first; simplify orchestration and reduce temporary allocations afterwards.

This audit adds this report and an offline evidence script. Production code, workflow configuration, published objects, and credentials were not changed. The pre-existing untracked `CODEC_BENCHMARK.md` was left untouched.

**Method and scope**

Read every tracked source, test, workflow, and configuration file: source discovery/download, native decoding, WCS chunk assembly and fallback, calibration, geometry, blending, zoom generation, AVIF encoding, pack layout, scheduling, R2 publication/retention, Worker authorization/signing/range reads/caching, and the dawn diagnostic. Compared documentation with the current code and checked the recent publication changes.

For suspected bugs, ran small deterministic reproductions with mocked network/storage boundaries and positive controls. Checked projection against PROJ, all pack addresses and byte ranges, and an actual AVIF encode/decode. Installed the exact `requirements.lock` into an isolated temporary environment; ran both existing Python suites there and the Worker suite with Node. Examined the pinned Satpy reader implementation to verify how incomplete Himawari scans are handled. Used official GitHub documentation to verify workflow concurrency and self-dispatch behavior.

Priorities reflect this deployment: **P1** affects ordinary rendered output and should be fixed promptly; **P2** affects recovery, failed downloads, or an explicitly supported operating mode; **P3** is a lower-priority correctness issue outside the current serialized workflow. These are code-level findings, not claims that the defects have been observed in live production.

**Findings, ordered by practical impact**

| Priority | Finding | Consequence |
|---|---|---|
| P1 | Fallback weights include sources with no usable channel/pixels | Valid backup satellite data becomes missing |
| P1 | WCS infrared nodata is mapped to valid temperatures | Missing MTG pixels can become false cold clouds |
| P2 | Partial Himawari downloads count as successful channels | Missing segments bypass the intended retry |
| P2 | Completely empty frames can be published | A failed acquisition can advance the app timeline to empty tiles |
| P2 | WCS requests exceed the remaining download budget | Optional imagery can delay the next live frame |
| P2 | Explicit starts merge against unrelated existing frames | Backfills are discarded or fail after their packs are uploaded |
| P3 | Manifest conditional writes do not cover the read used to assemble the timeline | A stale concurrent writer can replace revisions/history at the same latest time |

**1. P1 — Select fallback weights from usable data for each channel and pixel.**

Location: [mosaic_v2.py:832](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:832), especially the maximum score at line 848, the early cutoff at line 854, and the denominator threshold at line 898.

The maximum score comes from every candidate source, before checking whether it supplies IR or VIS and before sampling its valid pixels. A nearby satellite with VIS but failed IR still sets the IR normalization baseline. More distant valid IR is then discarded by `active = weight > 1e-6`, or by the final denominator threshold. The same problem occurs inside a present layer where its pixels are nodata, including a missing Himawari segment.

Evidence: a small Americas block has **715 valid IR pixels** with GOES-18 alone. Adding GOES-19 with VIS but no IR leaves **0 valid IR pixels**, while VIS remains populated. Giving GOES-19 an IR layer containing nodata produces the same result. This is a numerical selection error, not a lack of satellite coverage.

Fix: normalize each channel against sources that have valid sampled data at that pixel, and apply negligible-contribution cutoffs after that selection. Merely excluding a source with `ir is None` fixes only part of the problem. Keep the current view-angle preference, calibration, and blend curve for healthy overlapping data.

Compatibility: the wire format remains unchanged. Healthy-source scenes should retain their current appearance; scenes with failed channels or missing pixels gain the intended fallback coverage. Add regressions for both absent channels and pixel holes, plus a healthy-overlap control.

**2. P1 — Preserve GeoTIFF nodata before applying WCS calibration.**

Location: [mosaic_v2.py:630](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:630), with LUT application at lines 663 and 685.

`decode_geotiff()` discards the GeoTIFF nodata tag. All WCS IR LUTs map gray zero to a nonzero code. Cached real MTG crops in `results/dawn/` declare GDAL nodata tag **42113 = `0.0`**. Missing input zero maps to **MTG code 216, approximately 204 K**, which is a cold-cloud temperature; IODC maps to code 39 and FES to code 33. The renderer consequently treats missing data as valid, and it can displace real fallback observations. FES/IODC VIS also map gray zero to code 1 rather than missing data.

Evidence: a synthetic TIFF with the same declared nodata value passes through production decoding and FES coverage acquisition as a fully valid code-33 layer. The production MTG LUT maps that nodata value to code 216. The cached crop metadata supplies evidence for the MTG input convention; this audit did not re-download full-disk WCS imagery to count its live missing pixels.

Fix: preserve the declared nodata mask while decoding, then set those pixels to encoded zero after LUT application, consistently for IR and VIS and for chunked and unchunked sources. Masking by metadata avoids guessing whether a dark gray value is physically valid.

Compatibility: encoded zero already means missing data to the Worker/app. This corrects input decoding without changing temperature scales, display calibration, AVIF layout, or schema.

**3. P2 — Retry incomplete Himawari channels instead of accepting a successful partial decode.**

Location: [mosaic_v2.py:443](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:443), [mosaic_v2.py:495](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:495), and [mosaic_v2.py:526](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:526).

Discovery logs a segment count other than ten but still returns the objects. Download failures are removed from `grouped`, and any nonempty list is handed to Satpy. The retry set is based on whether decoding returned a layer, rather than whether all expected files arrived.

Satpy's Himawari configuration uses `GEOSegmentYAMLReader`, which fills missing segments with NaNs by default instead of requiring decoding to fail. This behavior is documented in the [Satpy segmented reader](https://satpy.readthedocs.io/en/stable/api/satpy.readers.core.yaml_reader.html#satpy.readers.core.yaml_reader.GEOSegmentYAMLReader) and was checked in the pinned 0.60.0 package source. A successful layer therefore does not demonstrate a complete download.

Evidence: when one of ten segment files is missing, a successful mocked decode causes the channel to enter `layers` after **one download pass**. No second pass occurs. The mock isolates the pipeline's retry decision; the reader-source check establishes why a partial decode can succeed in practice.

Fix: check the expected segment identities and successful downloads before considering an H09 channel complete. Retry the missing files/channels within the existing two-pass policy. After that, either omit the incomplete channel or explicitly use it as degraded coverage with valid fallback selection; do not silently call it a complete successful acquisition. This needs no generalized download framework.

**4. P2 — Reject a completely empty frame before upload/publication.**

Location: [publish_live.py:63](/Users/zhongzhi/SatellitePipeline/publish_live.py:63), [publish_live.py:71](/Users/zhongzhi/SatellitePipeline/publish_live.py:71), and [publish_live.py:103](/Users/zhongzhi/SatellitePipeline/publish_live.py:103).

Per-source download/decode failures can remove every usable layer. The renderer can still write a valid pack containing only its header and empty index. Neither `build_frame()` nor timeline validation checks that observations exist; the frame can replace the app's current timeline and count as a successful publication.

Evidence: the production build/publication path, with offline acquisition/render boundaries and an in-memory store, accepts **0 tiles and 0 coverage** as a published frame. Its 10,932-byte header/index satisfies the existing positive `pack_bytes` validation.

Fix: reject `stats['tiles'] == 0` before uploading and preserve the existing manifest. This is a simple guard against total failure. Do not introduce arbitrary global coverage percentages without deciding the intended degraded-source policy.

**5. P2 — Bound each WCS attempt and retry delay by the remaining budget.**

Location: [mosaic_v2.py:610](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:610), [mosaic_v2.py:614](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:614), and [mosaic_v2.py:626](/Users/zhongzhi/SatellitePipeline/mosaic_v2.py:626).

The shared deadline is checked before starting an attempt, but that attempt always receives a 120-second timeout and is followed by a retry sleep of up to 30 seconds. Executor shutdown waits for running requests. A request starting just before the deadline can therefore keep acquisition running beyond the configured 300-second budget and delay publication. FES and IODC are processed before MTG under that same budget, so slow earlier requests also reduce MTG's opportunity to complete.

Evidence: with one second of budget remaining, the mocked request receives **timeout=120**, followed by **a two-second retry sleep**. No real timeout was incurred during the audit.

Fix: use the smaller of the normal request timeout and remaining positive budget, skip attempts once the budget is exhausted, and cap retry sleeps to the remaining budget. Keep source ordering and the existing fallback policy unless measured logs justify changing them. A socket timeout alone is not a strict overall transfer timer, so verify elapsed acquisition time on the hosted runner after this change.

**6. P2 — Make explicit backfill/restart timeline selection independent of unrelated old frames.**

Location: [publish_live.py:226](/Users/zhongzhi/SatellitePipeline/publish_live.py:226) and [publish_live.py:301](/Users/zhongzhi/SatellitePipeline/publish_live.py:301).

`SATELLITE_START_UTC` enables rewinding, but publication merges all existing frames with the newly built frames, sorts them, and keeps the newest six. An older requested backfill is discarded when six newer frames already exist. A disjoint newer explicit start leaves a gap between retained old/new frames and fails `validate_timeline()` after download, rendering, and upload.

Evidence: adding a 21:00 backfill to an existing 22:00–22:50 timeline retains none of that backfill. Retaining 22:00 plus a requested 23:00 start fails continuity validation.

Fix: publish the requested explicit window, optionally extending it only with adjacent compatible frames. Validate timestamp alignment before expensive work. `allow_rewind=True` only bypasses storage ordering; it does not choose the intended timeline. This does not affect the normal scheduled path.

**7. P3 — Tie the conditional write to the manifest read used to assemble the timeline.**

Location: [publish_live.py:132](/Users/zhongzhi/SatellitePipeline/publish_live.py:132) and [r2_store.py:97](/Users/zhongzhi/SatellitePipeline/r2_store.py:97).

The orchestration reads frames, assembles a replacement, and calls the store. The store reads the manifest again and uses that newer ETag. A concurrent publication between those two reads is therefore not a precondition failure. The only remaining guard compares latest timestamps using `>`, allowing a stale replacement with an equal latest timestamp to shorten history or replace a newly published pack revision. It can also overwrite an intermediate concurrent extension when its own target is newer.

Evidence: an in-memory store accepts a one-frame stale 22:10 manifest over a two-frame 22:00/22:10 manifest, replacing the newest pack revision. Existing tests cover a race after the store's read, which is a different interval.

Fix if multiple publishers remain supported: carry the original ETag with the frames and use it for the write; on conflict, reread and reconstruct. Keep intentional backfills explicit. The deployed workflow serializes runs, so this is lower priority than the normal download/render defects. Do not add locks, ownership records, or a distributed queue to address it.

**Clean code, duplication, and simplification**

| Area | Practical recommendation | Reason / constraint |
|---|---|---|
| Timeline orchestration | After correctness fixes, simplify `complete_timeline()` around the serialized workflow: read current frames, add the built next frame, choose the contiguous window, conditionally publish. | The current workflow prevents the other workflow owner that the 30-minute wait/takeover machinery expects. Preserve storage conflict protection; determine whether anyone runs external publishers before removing overlap support. |
| Native discovery | Have readiness checking and downloading consume the same channel-selection/completeness helpers. | GOES matching and H09 channel/resolution checks are duplicated in `native_objects()` and `missing_inputs()`, and readiness uses `< 10` while discovery checks `!= 10`. |
| Bootstrap lookup | Reuse GOES hour listings within a single lookback search. | Multiple candidate slots in one hour currently fetch the same prefix repeatedly. Keep readiness polling fresh rather than caching across polls. |
| Native metadata | Either expose/log useful native diagnostics or remove their computation and propagation. | `source.info['ir'/'vis']` is populated but omitted from `source_summary()`; native shape/start time/load time/valid fraction are not reported. Avoid adding app-facing fields solely for implementation detail. |
| Download return values | Remove unused timing/byte/cache fields, or actually summarize them in logs. | `download_seconds`, `downloaded_bytes`, and `cached` are computed but never consumed by tracked code. |
| Resource lifetime | Use context managers or `try/finally` for the WCS executor and renderer globals. | `acquire_sources()` skips executor shutdown if native loading raises; `build_frame_pack()` skips clearing `_SOURCES`/`_WHEN` on failure. This is ordinary failure cleanup, not an elaborate recovery system. |
| Documentation | Correct the claim that the Worker rejects other encoding descriptors; document hourly recovery plus self-dispatch, and the 40-minute lag reseed. | The Worker checks pack headers but does not validate the manifest descriptor. The actual cron is hourly, and scheduled runs sometimes reseed rather than extend by exactly ten minutes. |

No entire tracked module is clearly dead. Keep `check_dawn.py`: its historical implementation is intentional comparison evidence, not accidental duplication of production logic. Keep the focused scheduling/storage modules; introducing more classes or a generic provider/plugin framework would make this code harder to follow.

If `mosaic_v2.py` continues growing, moving acquisition into one module would create a useful boundary. A broad file split before the behavior fixes would increase review churn without resolving the defects.

**Efficiency and operational improvements**

1. **Reduce lower-zoom temporary copies.** At z4, `build_frame_pack()` copies all 256 windows for both channels into `jobs`: **512 MiB** of image data, in addition to the two 16,384-square canvases, also **512 MiB**. `pool.map()` buffers results. Submit nonempty windows through a bounded/streamed iterator and collect them with their addresses. Start by skipping windows whose two channels are empty; avoid a shared-memory rewrite unless measurements still justify it. The memory totals follow directly from array sizes; production peak RSS was not measured here.
2. **Measure hosted-runner CPU threading before tuning worker counts.** Four render processes call BLAS-backed interpolation. Record backend/thread count, wall time, and memory on the actual Ubuntu runner before changing concurrency. The local locked-environment interpolation comparison favored the current dense implementation: about **3.61 ms** versus **13.35 ms** for an equivalent two-pass NumPy interpolation, with maximum absolute difference `1.79e-7`. A simpler-looking replacement was slower locally. These timings do not establish Linux performance.
3. **Keep the existing codec pending broader evidence.** The pre-existing AVIF/WebP benchmark supports AVIF size efficiency on its Europe/dawn sample. This audit did not repeat that benchmark or extend its geographic coverage. Switching format, resolution, quality, or code curves would affect the live app and is not a clean-code refactor.
4. **Add a small test-only CI workflow.** The only tracked workflow publishes production data and never executes the tests. Run the current suites and focused bug regressions on PRs without R2 credentials. Do not run full satellite acquisition in unit CI.
5. **Retain intentional scheduling tradeoffs.** The continuation job occupies a runner while sleeping and the workflow concurrency group remains held during that wait. At a healthy ten-minute cadence there are about 144 publisher runs and 144 continuation jobs per day. This spends runner time to avoid relying entirely on scheduled-trigger timing. GitHub explicitly supports self-dispatch with `GITHUB_TOKEN`; that is not a bug. The default concurrency queue has one pending slot, so cron/manual/continuation events can replace pending events. See [GitHub workflow triggering](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow) and [concurrency](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency). Preserve the working cadence unless actual cost or timing measurements justify a scheduler change.

Two small Worker observations do not warrant expanding the current audit into a security redesign. `/health` reports process health rather than publication freshness; freshness can be assessed from manifest timestamps. Also, successful client-manifest responses lack `Access-Control-Allow-Origin` even though OPTIONS and tiles include it, so a future cross-origin browser client would need that header. Neither establishes a failure in the present native app. Existing token checks occur before tile cache reads, which correctly avoids treating a cached response as authorization.

**Backward compatibility and rollout**

Preserve the current public contract: manifest `schema_version: 1`, `/v2/client-manifest` and `/v2/tiles/...`, WXSP pack version 3, z1–z5 addressing, 1,024-pixel map tiles, stacked 1,024×2,048 AVIF images with IR above VIS, encoded zero as nodata, the current IR/reflectance scales, existing manifest fields, and immutable frame/revision object names. These version numbers describe separate contracts and are not themselves inconsistent.

Reuse old frames only when the full encoding/grid contract is compatible. `read_published_frames()` currently checks just the zoom range; if tile size, encoding, or pack layout changes later, that check will be insufficient. No such format migration is needed for the recommended fixes.

Implement the nodata, fallback, partial-download, and empty-frame fixes in small reviewable changes with targeted regressions. Compare representative healthy and degraded scenes, including Europe dawn and the Americas. Keep the calibrated dawn behavior unless a comparison demonstrates a defect. Then address WCS timing and explicit-start behavior, followed by orchestration simplification and memory reduction.

Render any prepublication comparison into temporary/local packs or a separate storage prefix. Once validated, publish future frames with new revisions while keeping the current/previous-manifest retention behavior. Never overwrite packs that the app may have cached. The app source and live deployment state were not supplied in this repository, so this audit verifies the repository's wire contract and Worker behavior rather than certifying a particular shipped app build.

**Validation results and limits**

- Exact pinned Python dependencies installed successfully in an isolated temporary environment. `pip check`: no broken requirements.
- Python: **11 existing tests passed** across `test_r2_store` and `test_wcs_completeness`.
- Worker: **7 existing tests passed** using Node 23.1.0.
- Offline evidence: **7 findings reproduced**, with an additional missing-pixel variant for the fallback defect. Two positive controls passed: projection agreement for 10,000 points in each sweep mode, and actual AVIF layout plus every one of the 1,364 pack addresses and byte ranges.
- No full global render, native HSD/NetCDF fixture decode, hosted-runner benchmark, live R2 API request, deployment, or live-app end-to-end exercise was performed. This keeps the live pipeline untouched; those are appropriate final checks when implementing the fixes.

The original evidence script asserted defective behavior at the reviewed commit. Its fixed cases now assert corrected behavior in `test_pipeline.py`; no test asserts the deferred pixel-hole defect. Compatibility controls remain in `audit/check_contract.py`.

Reproduce from the repository using an environment containing `requirements.lock`:

```sh
python -m unittest discover -v
node --test worker/test/storage.test.mjs
python audit/check_contract.py
```

Current checks: [test_pipeline.py](/Users/zhongzhi/SatellitePipeline/test_pipeline.py) and [audit/check_contract.py](/Users/zhongzhi/SatellitePipeline/audit/check_contract.py).
