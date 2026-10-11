# AVIF versus WebP benchmark

Benchmark run on 7 October 2026. AVIF produced smaller files than WebP at
comparable measured quality in this sample, supporting the current AVIF format.

## Method

Compared 18 uncompressed 1024×2048 grayscale tiles containing stacked infrared
and visible satellite data. Inputs were cached MTG scans over Europe and the
Atlantic from 3 October 2026 at 06:50, 07:10, and 07:40 UTC, across six tile
locations at zoom levels 4 and 5.

The baseline used production AVIF settings: quality 50, speed 6, one encoder
thread. WebP quality was tuned separately for encoding methods 4 and 6, using
one fixed quality across all tiles per method. Each matched WebP setting met or
exceeded the baseline's aggregate pixel accuracy (PSNR), structural similarity
(SSIM), and PSNR of rendered clouds on a neutral background. Measurements used
the uncompressed references, rather than recompressing existing AVIF images.

## Results

Sizes below are totals for all 18 tiles.

| Encoding | Quality | Total size | Change versus AVIF | Code PSNR | Code SSIM |
|---|---:|---:|---:|---:|---:|
| AVIF, speed 6 | 50 | 510.2 KiB | Baseline | 47.23 dB | 0.99082 |
| WebP, method 4 | 90 | 1,209.2 KiB | +137.0% | 48.43 dB | 0.99150 |
| WebP, method 6 | 89 | 1,130.2 KiB | +121.5% | 48.09 dB | 0.99100 |

All 18 tiles were larger with matched WebP method 6, with individual size ratios
of 2.11–2.50 times AVIF. Lower WebP quality settings could produce smaller files,
but with more error than the AVIF baseline.

Lossless WebP, method 6 and effort 100, also produced larger files: 5.33 MiB
total, approximately 10.7 times the AVIF size. Exact decoded pixel identity was
verified for every lossless tile.

WebP encoded faster in the local lossy comparison: approximately 113 ms per tile
for method 4 and 161 ms for method 6, versus 296 ms for AVIF. These timings are
indicative, based on one encode per tile and setting with three parallel workers.

## Scope and environment

The sample covers Europe/Atlantic dawn scenes. It does not establish results
for global coverage, other satellites, noon, or night. Quality matching was
across the sample; individual tiles can differ. Zoom 5 samples used the
production rendering grid; zoom 4 samples were rendered directly at that
resolution, whereas published zoom 4 tiles normally average zoom 5 codes.

Local environment: macOS on ARM64, Python 3.12.0, Pillow 12.2.0, libavif 1.4.1,
and libwebp 1.6.0. Codec output was decoded to grayscale with Pillow. SSIM used
full-resolution 11×11 Gaussian windows, excluding missing-data regions and
the seam between the infrared and visible halves.
