# Performance measurements

## Baseline (M3, synchronous processing)

Measured on an Apple Silicon Mac, with the client and server on the same machine (so no network transfer time):

**Single large files:**

| Scenario | Image processing | Total request |
|---|---|---|
| One 43MB, 50-megapixel JPEG | ~0.8s | ~0.9s |
| Two of those uploaded at once | ~1.4s each | ~1.8s for both |
| `/healthz` during an upload | — | 2ms |

**A 200-photo trip import** (190 × 12-megapixel ~3MB JPEGs from a phone and a no-GPS camera, plus 10 screenshots; 598MB total):

| Upload pattern | Wall time | Throughput | Per request (p50 / p95) |
|---|---|---|---|
| One at a time | 84.9s | 2.4 photos/s | 405ms / 531ms |
| 4 at a time | 24.7s | 8.1 photos/s | 458ms / 699ms |

Where a single 12-megapixel upload's time goes: ~365ms image processing, ~8ms re-resolving all ~200 photos' times, and ~40ms for transfer, insert, and file writes.

What this shows:
- **Image processing is ~90% of the cost.** The O(n²) time re-resolution is negligible at trip scale: 8ms per call at 200 photos, extrapolating to roughly 400ms per call at 10,000. It matters for importing a year of photos, not a trip.
- **Threads do parallelize image work.** Pillow releases the GIL while decoding and resizing in C, so 4 concurrent uploads ran 3.4× faster on a 4-performance-core Mac, with each request barely slower. (Open question: the two concurrent 50-megapixel uploads showed no speedup. Possibly memory bandwidth, since each is ~150MB of raw pixels. Not verified.)
- **The server stays responsive** during processing, because it runs on the thread pool, not the event loop.
- Even at best, the client waits ~25s for a trip, with the work tied to open HTTP requests: if the browser tab closes, unprocessed photos are lost. M8's background worker decouples the two.

**M5 pass A: uploads through the API into object storage** (40 × 12-megapixel phone JPEGs, 119MB):

| Upload pattern | Wall time | Per request (mean) |
|---|---|---|
| One at a time | 20.7s | 497ms |
| 4 at a time | 9.3s | 876ms |

That's about 90ms more per photo than local disk (~410ms): three network writes (original, thumbnail, web copy) instead of three file writes. The first run after starting SeaweedFS was ~3× slower (58.5s) while it created storage volumes on first use, so benchmarks are taken warm.

**M5 pass B: presigned direct uploads** (same 40 photos, same session as a pass A re-run):

| Upload pattern | Pass A (through API) | Pass B (direct) | Pass B per photo: URL / PUT / complete |
|---|---|---|---|
| One at a time | 22.0s | 23.1s | 10ms / 67ms / 483ms |
| 4 at a time | 9.3s | 7.1s | 12ms / 82ms / 567ms |

On one machine, pass B isn't faster one at a time: moving 3MB over loopback takes ~70ms, and `complete` still downloads the original and processes it inline. The win is where the transfer time goes. Over a 10 Mbps home connection, a 3MB upload takes ~2.4s: in pass A that's 2.4s of an API worker per photo, and in pass B it's the bucket's problem while the API handles two small requests. The API's log shows only the upload-URL request and the `complete` call per photo (M6 moved both under `/imports`). M8 turns `complete` into "enqueue a job", making both requests genuinely tiny.

**M6: a 200-photo bulk import** (same synthetic trip as the M3 run; browser flow: hash, one `POST /api/imports`, 4 parallel uploads):

| Run | Hash | Register batch | Upload + process | Total | Result |
|---|---|---|---|---|---|
| 200 new photos | 0.9s | 1.5s | 58.3s | 60.7s | 199 imported, 1 duplicate, 0 failed |
| The same 200 again | 1.3s | 0.27s | nothing to send | 1.5s | all 200 skipped as duplicates |

The one duplicate in the first run was real: two generated screenshots were byte-identical. Re-importing a card costs seconds instead of a minute, since nothing is uploaded. Upload plus processing was slower than M5's rate would predict (~36s): each photo now also regroups trips over a growing set, hashes its bytes, and the machine's disk was ~99% full during the run, which slows storage writes. Which of these dominates isn't isolated yet; M8 removes the per-photo regroup, so re-measure then.

**M7: loading the world's places** (GeoNames `cities1000`, 171,094 rows; Natural Earth countries, 258 shapes):

| Insert method (same 20,000 rows) | Time | Rows per second |
|---|---|---|
| One `INSERT` per row | 8.32s | 2,403 |
| `executemany` (the driver batches the statements) | 0.64s | 31,370 |
| `COPY` | 0.03s | 592,770 |

`COPY` is ~250× faster than a statement per row: per-row inserts pay a network round trip and a statement parse every time, while `COPY` streams every row through one command. The full 171,094 places load in 1.4s. The country shapes take another second, mostly repairing their geometry.

**Nearest place: the index makes it ~800× faster.** Finding the nearest of 171,094 places to a point:

| Query | Time (warm) | How Postgres runs it |
|---|---|---|
| `ORDER BY location <-> point LIMIT 1` | 0.3 ms | Walks the GiST spatial index outwards from the point and stops at the first match |
| `ORDER BY ST_Distance(location, point) LIMIT 1` | 245 ms | Computes the distance to every row, then sorts (a parallel full scan) |

Both give the same answer. `<->` is PostGIS's distance operator, and unlike `ST_Distance` it's one an index can order by: `EXPLAIN` shows `Index Scan using places_location_idx` for one and `Parallel Seq Scan on places` for the other. The place data takes 41 MB with its index, and the country shapes 8 MB.

**M8 before: processing inside the request** (`python scripts/bench_import.py`: 200 synthetic files, 570 MB, 190 twelve-megapixel JPEGs and 10 screenshots; local SeaweedFS and the test database, same machine; same flow as the browser: hash, register the batch, upload 4 at a time, `complete` each):

| Phase | Time |
|---|---|
| Hash 200 files | 0.7s |
| Register the batch | 0.4s |
| Upload + `complete`, 4 at a time | 47.4s |
| **Total, until every photo is processed** | **48.5s** |
| One upload (PUT), median | 87 ms |
| One `complete`, median / 95th percentile | 812 ms / 1,167 ms |

The upload is 10% of each photo's time; the browser spends the rest waiting on `complete`, which downloads the original, decodes it, writes thumbnails, reads EXIF, resolves time and place, and regroups every trip. `complete` is slower than in M6 (median ~570 ms): each call now also matches places, and regroups an ever larger set of trips.

**M8 after: a worker processes in the background** (same script with `--wait`, same 200 files, one `python -m app.worker` with 4 threads beside the API):

| | Before | After | |
|---|---|---|---|
| One `complete`, median / 95th percentile | 812 / 1,167 ms | **52 / 209 ms** | ~15× faster |
| Browser's part (uploads and completes) | 47.4s | **10.5s** | 4.5× faster |
| Until every photo is processed | 48.5s | **33.3s** | 31% faster |
| Trip regroups | 200 | **6** | coalesced |

`complete` now checks the file is in storage, marks it uploaded, and queues a job; the worker does the rest. The browser is done in 10.5s and can be closed; the server finishes on its own. Processing itself also got faster: regrouping every trip after each of 200 photos (each time over more photos) became 6 regroups, since a regroup request is absorbed by one already waiting. All 200 jobs succeeded first time, with no retries.
