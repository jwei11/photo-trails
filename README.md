# Trip Mapper

Drop in the photos from a trip. It reads the timestamps and GPS out of them, works out where you went and when, and puts it on a map.

Built milestone by milestone following [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md).

## Setup

### Run it (Docker only)

Needs Docker with Compose, and nothing else: no Python, Node, or AWS account.

```bash
git clone <this repo> && cd photo-trails
cp .env.example .env     # then replace every change-me (letters and digits only)
docker compose up --build
# in another terminal, once it's up: create your account and print a one-time sign-in link
docker compose exec api python -m app.users create you@example.com --login-link
```

Open the printed link (it's valid for 15 minutes) and press the button to sign in. The first start builds the image (about a minute), applies the database migrations, and downloads and loads the place data in the background (about 23 MB; photos get place names once it's done). Photos are stored in the local SeaweedFS. To use an Amazon S3 bucket instead, see **Photo storage** below. Stop with Ctrl-C, or `docker compose down` if it was started with `-d`. The data stays in Docker volumes until `docker compose down -v`.

```
           browser: http://localhost:8000 (app + API)   http://localhost:8333 (signed photo URLs)
 ─ host ──────────────────┼──────────────────────────────────────┼──────────────────────
 compose network:         ▼                                      ▼
                         api ───────────────────────────────▶ objects (SeaweedFS)
                          │                                      ▲
                          ▼                                      │
   migrate (exits) ──▶   db (Postgres + PostGIS)  ◀──────────  worker
   geodata (exits) ──▶
```

`api`, `worker`, `migrate`, and `geodata` all run the one image built from the `Dockerfile`, with different commands. `migrate` applies migrations and exits, and the API and worker start only once it has succeeded. `geodata` loads place data if none is loaded, and exits. The ports are published on 127.0.0.1 only; if one is taken, set `DB_PORT`, `OBJECTS_PORT`, or `API_PORT` in `.env`.

To check a stack end to end (it uploads a test photo, so only on a fresh one): `docker compose up -d --build && docker compose wait geodata && python3 scripts/smoke_compose.py`.

### Develop

For development, run the database and storage in Docker and the API, worker, and frontend directly on your machine, where they reload on every save:

```bash
cp .env.example .env            # then set real passwords
docker compose up -d --wait db objects  # Postgres + PostGIS, and local S3-compatible storage
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/alembic upgrade head  # apply migrations
.venv/bin/python -m app.geodata load    # place data, once (downloads to data/geo/)
.venv/bin/python -m app.users create you@example.com --login-link  # your account
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m app.worker  # in another terminal: processes uploads in the background
```

Don't run the whole stack (`docker compose up` with no service names) at the same time: its API also wants port 8000.

Without a worker running, imported files wait at "processing" (they're queued, not lost) until one starts. The worker also runs the orphan sweep every 15 minutes. Stop it with Ctrl-C: it finishes the jobs in progress first.

The frontend is a React app in `frontend/` (needs Node 20.19+ or 22.12+). For development, run Vite's dev server alongside the API and open http://localhost:5173. It reloads on every save and forwards `/api` to the API on port 8000:

```bash
cd frontend && npm install && npm run dev
```

To serve it from the API instead, as in production, build it once with `npm run build` and open http://localhost:8000. The API docs are at http://localhost:8000/docs.

**Accounts** are invite-only: there's no sign-up page. Invite someone with `python -m app.users create their@email` (inside Docker: `docker compose exec api python -m app.users ...`), and they sign in with Google using that address. `python -m app.users login-link their@email` prints a one-time link instead, which works without Google; the links point at the first `APP_ORIGINS` entry. A database from before accounts (M10 or earlier) needs an owner for its photos: run that one migration as `OWNER_EMAIL=you@example.com .venv/bin/alembic upgrade head`, and it creates that user and gives them everything.

**Sign in with Google** is optional and set up once in the Google Cloud console (free, no billing account needed): create a project; under Google Auth Platform, set up Branding (app name, your email), set Audience to External and leave it in Testing with yourself and your invitees added as test users; then under Clients, create an OAuth client of type Web application with the authorized redirect URIs `http://localhost:8000/api/auth/google/callback` and `http://localhost:5173/api/auth/google/callback`. Put its client ID and secret in `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. The app asks Google only for `openid email`.

**Photo storage** is the local SeaweedFS by default. To use an Amazon S3 bucket, set one up once in the AWS console, before anything else: a billing budget (the Zero spend template plus a small monthly one), then a bucket in `S3_REGION` with Block all public access on, a CORS rule allowing `GET` and `PUT` with a `content-type` header from `http://localhost:5173` and `http://localhost:8000`, and an IAM user whose only permissions are `s3:ListBucket` on the bucket and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on its objects. Put that user's access key in `.env`, with `S3_ENDPOINT` and `DOCKER_S3_ENDPOINT` both empty (see `.env.example`). The app checks it can reach the bucket at startup and says why if it can't. To copy photos from SeaweedFS into the bucket: `.venv/bin/python -m app.copy_storage --dry-run`, then without `--dry-run`. It's safe to re-run and never deletes anything.

Run the tests: `.venv/bin/python -m pytest`. They always use the local SeaweedFS, never S3, and refuse to start if `TEST_S3_ENDPOINT` points anywhere else, because the API tests empty their bucket. The API tests need their own database, because they wipe it. Create it once with `docker compose exec db sh -c 'createdb -U $POSTGRES_USER trips_test'` and set `TEST_DATABASE_URL` in `.env`. Without it, those tests are skipped. Don't run a worker against the test database while testing (as `scripts/bench_import.py` setups do): the tests run queued jobs themselves, and a worker would take some of them, failing tests at random. A worker on the dev database is fine.

Lint: `.venv/bin/ruff check .` and `cd frontend && npm run lint`. CI (`.github/workflows/ci.yml`) runs both lints, both test suites, and the Docker smoke test on every pull request and every push to `master`.

Frontend tests: `cd frontend && npm test`. They render components in a simulated browser with a fake API, so they need neither the server nor the database.

Reset all data from scratch (database, stored photos, and downloaded place data): `docker compose down -v && docker compose up -d --wait db objects && .venv/bin/alembic upgrade head`

## How a photo gets its time

"When was this taken" is two questions: the **instant** (an absolute UTC moment, used for ordering) and the **local zone** (what the wall clock said there, used for display). Cameras record neither reliably: `DateTimeOriginal` is a wall-clock reading with no zone.

The **zone** comes from, in order:
1. The GPS coordinates, via an offline timezone-boundary lookup (`timezonefinder`).
2. The nearest photo in time that has coordinates, if one is within 48 hours. This is how a no-GPS camera shooting alongside a phone gets placed.
3. The offset the camera recorded (`OffsetTimeOriginal`), as a fixed offset.
4. `DEFAULT_TIMEZONE`, flagged in the UI as a guess.

The **instant** comes from the best available source, and `time_source` records which one:
1. `exif_offset`: camera time minus the offset the camera recorded (most phones write this).
2. `gps`: GPS satellite time.
3. `coordinates` / `neighbour` / `default_zone`: camera time interpreted in the zone found above.

These are independent. A camera left on New York time in Lisbon records `09:00 -04:00`: the instant is right (13:00Z), and it displays as 14:00 in Europe/Lisbon because the zone comes from the GPS.

**Clock drift.** A wrong camera clock is corrected with a per-camera offset (make + model + serial), optionally limited to a range of camera-clock times. The raw `camera_time` is never modified; `shot_at` is recomputed from the raw values plus the offsets, so corrections are reversible.

The logic lives in `app/timeresolve.py` as pure functions with no database or files, and is covered by `tests/test_timeresolve.py`.

## How photos become trips

Dated photos are walked in time order, and a new trip starts when:
- photos are more than **48 hours** apart, or
- they're more than **300 km** apart **and** more than **24 hours** apart, or
- a photo taken **at home** falls between them.

"At home" means within the radius (default 25 km) of any home you've set. A photo without GPS takes the at-home status of the nearest photo in time. Home photos, undated photos, and groups of fewer than **3 photos** aren't trips; they're shown as "Not in a trip", not deleted.

The algorithm is sometimes wrong, so fixes are one click: rename, merge, split at a photo, remove a photo, or "not a trip". Any edited trip becomes **locked**. Re-running the algorithm (after every upload, clock correction, or home change) only regroups photos outside locked trips, so manual work is never undone. A late photo that falls inside a locked trip's time span (say a friend's, imported later) joins it. Photos removed by hand are pinned out and never regrouped.

Trip IDs are stable across re-runs: a regrouped set of photos keeps the ID that most of its photos already had. Share links (M11) will depend on this.

The logic is in `app/trips.py` (pure functions, `tests/test_trips.py`), and the edit operations in `app/trip_store.py` (`tests/test_trips_api.py`).

## Decisions

**IDs are UUIDs generated by Postgres.** Sequential integers leak how many photos exist and can be enumerated (`/photos/1`, `/photos/2`, ...). Generating the ID in the database (`gen_random_uuid()`) means every row gets one even if it's inserted outside the app. The tradeoff is that the app needs the ID before naming the file, so the upload inserts the row, writes the file, and commits in one transaction. If the file write fails, the row rolls back.

**Stored filenames never come from the client.** Files are stored as `<uuid><ext>`, and the extension must match `\.[a-z0-9]{1,10}`. Client filenames collide and can contain path traversal (`../../`). The original name is kept only as metadata.

**Columns vs. JSONB.** Rule: if we filter, sort, or join on it, it's a column; otherwise it goes in JSONB. M2 only has columns because everything stored so far is either queried or small and fixed. M3 adds EXIF: `shot_at`, `lat`, and `lon` become columns (timeline sorting, map queries), and the full tag set goes in a JSONB `exif` column. Camera models vary too much for a fixed schema, and we rarely query individual tags.

**PostGIS is installed from day one, in our own image.** M7 needs the extension, and switching images later would mean recreating the data volume. The official `postgis/postgis` image has no arm64 build, so on Apple Silicon it would run under slow amd64 emulation. Instead, `docker/db/Dockerfile` installs Debian's `postgresql-17-postgis-3` package on the official `postgres:17` image. It's native on both Intel and ARM and uses only official sources. Rejected: `imresamu/postgis` (a third-party image in the supply chain) and amd64 emulation (too slow for M7's bulk loads).

**Alembic for migrations, SQL via SQLAlchemy Core (no ORM).** Migrations are hand-written SQL inside Alembic revisions, so the exact DDL is visible. Queries are plain SQL strings with bound parameters. PostGIS queries (M7) are much clearer in SQL than through an ORM. SQLAlchemy supplies connection pooling and parameter binding. The tradeoff is no autogenerated migrations, so every schema change is written by hand.

**Pillow + pillow-heif for reading photos.** We need Pillow for thumbnails anyway, and pillow-heif adds iPhone HEIC. Both are pip installs and run in-process. Rejected: ExifTool (reads everything including RAW and video, but it's an external Perl program called per file and must be installed in every environment) and exifread (no HEIC support, and we'd still need Pillow).

**Time resolution uses a fallback chain and records its source.** See "How a photo gets its time" above. Rejected: coordinates only (ignores better data and fails for no-GPS cameras) and asking the user for one zone per trip (wrong for multi-country trips).

**Raw metadata and derived values are separate columns.** `camera_time`, `exif_offset_min`, `gps_time`, `lat`, and `lon` are what the file said and are never changed. `shot_at`, `tz_name`, `local_offset_min`, and `time_source` are computed from them. Any correction (a clock offset, or a better neighbour arriving later) recomputes the derived values without losing the originals.

**Overlapping clock offsets are rejected by the database.** One camera can't have two offsets covering the same camera-clock times, because the answer to "which one applies?" would be a hidden rule. An exclusion constraint (`EXCLUDE USING gist ... tsrange(valid_from, valid_to) WITH &&`) enforces it, and the API turns a violation into `409 Conflict`. A check in Python would race: two concurrent requests could both pass it before either saves. Ranges are half-open, so one ending exactly where the next begins is allowed. Rejected: stacking overlapping offsets (the effective correction is spread across rows) and letting the newest win (old rows linger and look active).

**Replacing a clock offset is one request and one transaction.** The Settings page tries to add a correction; if it overlaps an existing one, the 409 makes it ask, and on yes it sends the same request with `replace_overlapping: true`. The server then deletes the camera's offsets that overlap the new range and inserts the new one in a single transaction, using the same overlap test as the exclusion constraint, so exactly the rows that would block the insert are removed and corrections for other dates are kept. The Phase 1 page deleted the old offset and created the new one in two requests: if the second failed, the camera silently lost its correction and its photos moved back to the wrong times (tested: a failure after the delete leaves the old offset in place). Rejected: computing overlaps in the browser (duplicates the constraint's rule, and races with other edits) and `PUT` on the whole collection (replaces every offset, including other dates').

**Photos with no time information are kept and marked undated.** `shot_at` is NULL and they appear in their own section. Rejected: using the file's modified time (copying a file changes it, so it's a silent wrong answer) and rejecting the upload (dumping a camera roll would fail on every screenshot).

**Resolution recomputes every photo, serialized by an advisory lock.** A new photo can change its neighbours' zones, so each upload re-runs resolution over all photos and updates only rows whose result changed. `pg_advisory_xact_lock` makes concurrent uploads take turns, because two resolutions running at once would overwrite each other's results. Across a bulk import this is O(n²), but measured cost is 8ms per call at 200 photos (see the performance baseline), so it only matters at thousands of photos. M8 moves it to a single job that runs after an import finishes.

**WebP derivatives with all metadata stripped.** 400px thumbnails and 2048px web copies, about 25–35% smaller than JPEG at the same quality, and supported by every current browser. Derivatives are written with no EXIF, so no GPS leaks through a shared image (tested). The ICC colour profile is kept so iPhone Display P3 colours stay accurate. Orientation is applied to the pixels, since the tag that carried it is stripped.

**Trips: time gap + distance jump + homes.** The guide's starting point is a time gap alone, but a year of photos also includes everyday life at home, which would become dozens of fake trips, so you set one or more homes by hand (auto-detecting home is M11). Multiple homes, because a student has at least two. Plain "300 km apart" would split every multi-city trip at each flight or train ride (Lisbon to Madrid is 503 km), so a distance jump only counts after 24+ hours without photos: the case it's for is two separate trips back to back with no home photos between them. Rejected: time gap only (a fast move between trips stays merged) and distance alone (multi-city trips need manual merging).

**Edited trips are locked rather than edits being replayed.** Storing each fix as a rule ("these photos together") and re-applying it after every run is more flexible, but rules conflict with new photos in ways that are hard to explain. Locking has one rule: if you touched it, the algorithm doesn't. Rejected also: grouping once per import (a friend's late photos would never join the right trip).

**Minimum of 3 photos per trip, and undated photos stay unassigned.** Without a minimum, stray single photos each become a trip. Undated photos have no position in time, so any placement would be a silent guess (guessing from filename order breaks across cameras); you move them by hand.

**Distances use the haversine formula in Python.** It's a small, fast, testable pure function, accurate to about 0.5% (it treats the Earth as a sphere), which is plenty for "300 km apart". PostGIS takes over for geographic queries in M7.

**All writes take one Postgres advisory lock at the start of the transaction.** Every write ends by recomputing times and trips from the whole photo set. Without the lock, an upload's regroup could read the trips before a concurrent merge committed and overwrite the merged photos with stale assignments. Taking the lock first, not just before the recompute, closes that gap. Image processing happens before the transaction, so uploads still process in parallel.

**Undecodable files are stored, not rejected.** RAW files, videos, and corrupt uploads are kept as originals with `processing_error` set. They aren't lost, and they can be reprocessed if support is added.

**Photos live in S3-compatible object storage, run locally with SeaweedFS.** Local disk ties files to one machine: a second API server, a worker (M8), or a redeploy can't see them. All storage goes through boto3 (the AWS SDK), so production can use Amazon S3, Cloudflare R2, or DigitalOcean Spaces by changing the endpoint and credentials. The guide suggests MinIO, but MinIO stopped publishing images in 2025. SeaweedFS is maintained and supports everything used here (presigned URLs with signed headers, CORS). It only publishes a moving `latest` tag, so compose pins the image by digest. Rejected: Garage (more setup steps before first use) and building MinIO from source (we'd own security updates for an abandoned image).

**Images are served by redirecting to short-lived signed URLs.** `GET /api/photos/{id}/thumb` returns `302` to a presigned bucket URL valid for an hour, so image bytes never pass through the API and the bucket stays private. Signed URLs differ on every request, which defeats browser caching, so the redirect itself is cacheable (`Cache-Control: private, max-age=3000`): repeat views reuse the same URL and hit the browser's image cache. Rejected: streaming through the API (every image view loads the API) and a public bucket (originals include GPS, and anyone with a URL could see them forever).

**Uploads go straight from the browser to storage (presigned PUT).** The browser asks for an upload URL (`POST /uploads` with filename, type, and size), PUTs the file directly to the bucket, then calls `POST /uploads/{id}/complete`. The API never holds file bytes during transfer, so slow client connections tie up the bucket instead of API workers. Size and content type are part of the URL's signature, so the bucket rejects any other file (tested). Rejected: presigned POST with a policy (more flexible limits, but a multipart form, and support varies more across S3-compatible stores). `POST /api/photos` still accepts a direct upload for scripts and tests, and runs the same steps internally.

**Photos are `pending` until completed, and completion is idempotent.** A row is created when the URL is issued, so the storage key exists before the file does. Pending photos are invisible everywhere else. `complete` can be retried safely: it returns the existing photo if already done, derivative keys depend only on the photo ID so a retry overwrites instead of duplicating, and the status is re-checked under the write lock so two concurrent completions don't both finish.

**Orphans are swept by `python -m app.cleanup`.** It deletes pending uploads with no upload URL issued in the last hour, plus bucket objects no row references (left by a failed commit: the dual-write gap). Age is measured from the **last upload URL issued**, not from when the row was created: an import creates every pending photo up front, and a long import's last files may not start uploading for over an hour. Since URLs expire after 15 minutes and clients get a fresh one before uploading, an active upload always has a recent URL, so the sweep never races it. (The first version measured from creation; review caught that it would break long imports.) The delete re-checks both the status and the age, so a completion or a URL renewal that lands between the sweep's read and delete isn't lost. `--dry-run` reports without deleting. For now it's run by hand; M8's worker will schedule it.

**Imports are tracked on the server, one row per file.** A bulk import registers the whole batch in one request (`POST /api/imports`), and each file then moves through `uploading → done | duplicate | failed`. A failure records its stage: `upload` means the file must be sent again; `processing` means it's already in storage and completing again retries on the server with no re-upload. Because the server holds the state, one file failing doesn't affect the others, failures survive a page refresh, and an interrupted import resumes by picking the same files again (matched by hash). Rejected: tracking in the browser only (much less code, but a refresh loses the record of what failed). M8's import-level progress and "group trips once per import" build on this. The single-file upload endpoints from M5 were folded in: one file is an import of one.

**Duplicates are identical bytes, detected by SHA-256 in the browser and verified on the server.** The browser hashes each file before registering the batch: files already imported are skipped without being uploaded, and a file picked twice in one batch is only sent once. The server doesn't try to resolve repeats within a batch in advance, because marking the second copy a duplicate before the first has arrived would report a photo that might never exist. If another client sends the same file twice, both upload and the normal completion check marks whichever finishes second as the duplicate. Re-importing a whole memory card costs a few seconds of hashing, not a full upload. The client's hash is only used to skip work: during processing the server hashes the bytes actually in storage, and if they match an existing photo, the upload is discarded and the item is marked as a duplicate of the original. A unique index on the verified hash is the final guard against two identical files completing at the same moment. Rejected: hashing only on the server (every duplicate is uploaded in full first) and a storage-enforced checksum in the signed URL (elegant, but support varies across S3-compatible stores). Duplicates are skipped and linked to the original rather than kept and flagged, since identical bytes add nothing. Near-duplicates (a re-exported or edited copy) aren't detected; that's perceptual hashing, in M15.

**The browser uploads 4 files at a time, with one automatic retry.** Four matches the parallelism measured in M3. Each file's PUT is retried once with a fresh upload URL, which covers both brief network blips and URLs that expire (after 15 minutes) partway through a long import. A second failure is recorded and shown with a Retry button.

**Each timestamp is compared against the clock that wrote it.** The sweep first compared the database's `created_at` against a cutoff from the app's clock, and a test failed intermittently: the Docker VM's clock runs ~40ms ahead of the host's, so a just-created row looked newer than "now". Row cutoffs are now computed in SQL with the database's `now()`. Bucket timestamps come from the store's clock; with a one-hour threshold, millisecond skew is irrelevant.

**Two S3 endpoints: one the API uses, one baked into signed URLs.** A presigned URL's signature covers its hostname. Once the API runs in Docker (M10), it reaches storage at an internal name (`objects:8333`) that browsers can't resolve, while browsers need `localhost:8333` or a public domain. Signing with the internal client would produce URLs that are valid but unreachable. `S3_ENDPOINT` and `S3_PUBLIC_ENDPOINT` are the same today, and configured separately from the start.

**Photos move to Amazon S3; development uses a real dev bucket, and tests stay on SeaweedFS.** Phase 1 used SeaweedFS for everything. Using a real S3 bucket during development means IAM permissions, CORS, and request signing are exercised now, not discovered all at once at deploy time. Each environment gets its own bucket (a dev bucket now, a production one at M12), so development can never touch production photos. Tests keep using a local SeaweedFS bucket: they wipe it, run in seconds, cost nothing, and will run in CI without AWS credentials. Rejected: SeaweedFS until deploy (no AWS cost now, but every S3 difference surfaces during M12) and S3 for tests too (slow, costs money, and CI would need AWS keys). The region is us-east-2 (Ohio). Oregon (us-west-2) is closer, but AWS's free plan doesn't offer it; using it would mean switching to a paid plan, which can wait until the M12 deploy. The extra ~60 ms per request from California doesn't matter for loading photos, and what will matter at M12 is putting the server in the same region as the bucket. The startup check compares the bucket's real region with `S3_REGION`: boto3 quietly follows S3's redirect to the right region for its own calls, so a mismatch otherwise surfaces only as every browser link failing (it happened while setting this up).

**The bucket and its CORS rule are set up outside the app.** On S3 the app's credentials can only read, write, list, and delete objects in its one bucket; it can't create buckets or change their settings, so a leaked key can't make the bucket public or rewrite its CORS rule. The bucket is created in the AWS console now (Block Public Access on, ACLs disabled, SSE-S3 encryption) and moves to Terraform in M12. At startup the app only checks that it can reach its bucket and stops with a clear error if not. Against SeaweedFS the app still creates the bucket and its CORS rule itself, as a local convenience. Rejected: the app configuring the bucket at startup (as in Phase 1), which needs `s3:CreateBucket` and `s3:PutBucketCORS` on the app's own keys.

**The app authenticates as an IAM user limited to one bucket.** Its access keys live in `.env` (never committed). The policy allows `s3:ListBucket` on the bucket and `s3:GetObject`, `s3:PutObject`, and `s3:DeleteObject` on its objects, nothing else. Presigned URLs carry this user's permissions, so a browser holding one can do exactly one operation on one key until it expires. Rejected: IAM Identity Center (short-lived credentials from `aws sso login`, safer on a laptop, but more setup, and the M12 deploy to a non-AWS VM still needs another approach). Long-lived keys are the main risk: if one leaks, deactivate it in IAM and create a new one.

**Existing photos are copied to S3 with the same keys.** Database rows store each object's key, not its location, so copying every object to the same key in the new bucket keeps every row valid with no migration. The copy script runs as a dry run first and never deletes from the source. Rejected: starting with an empty bucket (existing photos' images would break).

**The frontend is a React single-page app built with Vite.** Phase 1's single plain-JS page was fine for lists and buttons, but the M9 map and timeline scrubber need shared state across many interactive parts, which is what React is for. Vite compiles the app to static files, so FastAPI stays the only backend. Rejected: Next.js (its server rendering helps search engines and first loads on public pages, but it adds a Node server beside FastAPI, and Leaflet needs the browser's `window`, so map components would need client-only workarounds) and staying with plain JS (the map would be hand-written DOM updates).

**TypeScript, not JavaScript.** API responses have many nullable fields (a photo may have no `shot_at`, no coordinates, no trip), and types make the compiler catch a forgotten null check or a misspelled field before the page runs. Rejected: plain JavaScript (less to learn up front, but those mistakes show up as blank UI at runtime).

**FastAPI serves the built frontend.** In development, Vite's dev server (port 5173) serves the app with hot reload and forwards `/api` requests to FastAPI. In production, FastAPI serves Vite's output folder, so the page and the API share one origin: no browser CORS rules between them, and one container to deploy. M10's multi-stage Dockerfile can build the app with Node and ship only the static files. Rejected: a separate nginx container for the frontend (independent deploys, but one more service and a reverse proxy or CORS in front of both, for no benefit at this size).

**The API lives under `/api`.** The React app has its own page URLs (`/trips/abc`), and with the API at the root those would collide with API routes of the same path. A prefix separates them: everything under `/api` is data, everything else is the app, and the dev server needs one forwarding rule. `/healthz` stays at the root, since it's for infrastructure, not the app. Rejected: hash URLs (`/#/trips/abc`), which need no backend change but look dated and make M11's share links awkward.

**Frontend libraries: React Router, TanStack Query, CSS Modules, in `frontend/`.** The React project is its own folder at the repo root with its own `package.json`, so the Python package stays Python-only. React Router maps page URLs to components; it's the standard choice. TanStack Query owns every API call: it caches responses, tracks loading and error states, refetches data after an edit changes it, and can poll, which M8's import progress needs. CSS Modules are plain CSS files whose class names are scoped to one component automatically, so styles can't leak between components. Rejected: TanStack Router (type-checked routes, but newer and more to learn), hand-written `fetch` in `useEffect` (every component re-implements loading, errors, and refetching), and Tailwind (fast once learned, but utility classes crowd the markup and add a second styling language to learn alongside React).

**FastAPI sends `index.html` for any URL that isn't the API or a built file.** React Router picks the page from the URL in the browser, but a reload or a pasted link to `/trips/abc` asks the server for that path first. So when routing finds nothing for a GET or HEAD, FastAPI's 404 handler serves the real file from `frontend/dist` if there is one, and `index.html` otherwise. It hooks in after routing rather than being a catch-all route, which a first version did, and review caught two bugs in it. A catch-all matches every path, so FastAPI's own routing answers never happened (`/api/photos/` stopped redirecting to `/api/photos`, and a POST there got 405 instead). It also crashed with a 500 on a URL containing a null byte (`/%00`), which the handler treats as just another page URL. Unknown `/api/...` paths still get a JSON 404, because answering a bad API call with a web page would hide the bug. Paths are resolved and checked to stay inside the build folder (`/%2e%2e/` arrives decoded as `../`; tested). Vite puts a content hash in asset filenames (`index-CiJeS6AA.js`), so assets are cached for a year, while `index.html`, which names the current hashes, is re-checked on every load. A deploy is then picked up on the next page load without anyone clearing their cache.

**Frontend tests use Vitest and React Testing Library.** Vitest is Vite's own test runner, so tests compile TypeScript and CSS Modules exactly as the app does, with no separate configuration. Testing Library renders a component and drives it the way a user would: find the button by its label, click it, check which API request went out and what the page shows afterwards. Tests don't depend on internal state or markup structure, so refactoring a component doesn't break them. Rejected: no frontend tests until CI in M12 (the backend's tests can't see a button that sends the wrong request) and Jest (the long-time default, but it needs its own TypeScript and CSS setup alongside Vite's).

**A running import lives in an engine outside React, on its own `/import` page.** An import is a process, not a page of data: it hashes files, registers the batch, and runs four uploads at a time for minutes, and the browser holds the `File` objects the server never sees. `frontend/src/imports/importEngine.ts` is a plain TypeScript class that owns all of that and publishes an immutable snapshot of its state on every change. Components subscribe with React's `useSyncExternalStore`, so they re-render when the snapshot changes. Because the engine isn't a component, leaving the import page mid-import doesn't lose anything: uploads continue, the header shows progress on every page, and coming back shows the full state. Its network, hashing, and upload functions are passed in, so tests drive it with fakes and no rendering. Rejected: state in the import component with `useReducer` (simplest, but navigating away unmounts it and throws away the progress of uploads that are still running) and Zustand (a small library that does the same with less code, but it hides the subscribe mechanism this is meant to show).

**Cameras and homes are on one Settings page.** Both are rarely changed and both change how every photo is read: when it was taken (clock corrections) and whether it belongs to a trip (homes). Keeping them off the timeline keeps that page about photos. Clock corrections can be limited to a range of camera-clock times with a note, which the Phase 1 page couldn't do. Rejected: a page each (two nav links for two short forms) and panels above the timeline (where the old page had them; the timeline gets long).

**Phase 2 order: React, then S3, then M7–M9.** The guide places the frontend switch at M9's map. Moving it to the start means M7's place names and M8's import progress are built once, in React, instead of in the old page first. S3 comes next because it's mostly configuration, and M8 may use SQS, which needs the same AWS account set up with a billing alarm.

**Places come from GeoNames `cities1000`, countries from Natural Earth borders.** `cities1000` lists about 150,000 places with at least 1,000 people, down to villages, with coordinates, population, country, and region codes. It's free, offline, and small enough to load in seconds. The country a photo is in is decided by which Natural Earth country shape contains it, not by the nearest place's country: near a border or a coast, the nearest named place is often across the line. Rejected: `cities15000` (about 30,000 places: rural photos snap to a town 30+ km away) and `cities500` (more obscure places to weight away, for little gain), and a reverse-geocoding API such as Nominatim or Google (rate limits, API keys, cost at volume, and every photo's location sent to a third party).

**A photo's place and country are stored on it and recomputed, like its time.** They're derived from the raw `lat`/`lon` and the place data, so they're recomputed whenever either changes, and never edited directly. Stored values make timeline and trip pages cheap, and M11's location fuzzing and M14's places build on them. Rejected: looking places up in every query (always current, but the same nearest-neighbour searches on every page load).

**Place data is loaded by a script using Postgres `COPY`.** `python -m app.geodata load` downloads the files, parses them, and streams the rows into Postgres with `COPY`, which sends rows as one continuous stream instead of one `INSERT` statement per row. Tables are created by an Alembic migration; their contents are loaded by the script, because a migration should describe the schema, not download 30 MB. Rejected: loading inside a migration (every `alembic upgrade` would fetch data) and GDAL's `ogr2ogr` (the standard tool for geodata, but a system dependency every environment would need).

**A photo is named after the place it's deepest inside, scaled by population.** The nearest named place is often wrong as a label: the nearest place to the Torre de Belém is Restelo, a neighbourhood of 7,000, and to the Eiffel Tower "Paris 16 Passy". So each place claims a radius of 20 m × √population (Lisbon 14 km, a neighbourhood of 12,000 2.3 km, a village of 1,000 630 m), and the photo is named after the place whose radius it's deepest inside: the smallest distance ÷ radius. If no radius reaches the photo, it gets the nearest place. The nearest place is stored too, as detail ("Baixa, Lisbon"). Four rules were compared on real spots before choosing:

| Spot | Nearest | Nearest, skipping neighbourhoods | Biggest place in reach | **Deepest inside (chosen)** |
|---|---|---|---|---|
| Torre de Belém | Restelo | Algés | Lisbon | **Lisbon** |
| Praça do Comércio | Baixa | Baixa | Lisbon | **Lisbon** |
| Puerta del Sol | Sol | Madrid | Madrid | **Madrid** |
| Pena Palace, Sintra | Sintra | Sintra | Algueirão (a suburb) | **Sintra** |
| Eiffel Tower | Paris 16 Passy | Paris 16 Passy | Paris | **Paris** |
| Remote beach | Ulgueira | Ulgueira | Ulgueira | **Ulgueira** |

"Biggest place in reach" was the first fix and failed on the Pena Palace: a large suburb's radius reached it and outvoted the town it actually belongs to. Dividing by the radius lets a small town win right next to it. Versailles and the Brooklyn Bridge ("New York City", not "Financial District") also come out right. Some spots stay judgment calls (the Torre de Belém scores 0.48 for Lisbon against 0.53 for Restelo), and the 20 m factor is a tuning knob. It costs about 3 ms per photo, against 0.3 ms for nearest only, because every candidate within 50 km is measured.

**A photo's country is the Natural Earth shape containing it, or the nearest within 12 nautical miles.** At this scale coastlines are approximate: a point on the Cascais shoreline is 733 m outside Portugal's shape. Within 22.2 km of a coast (a country's territorial waters), a photo takes the nearest country; further out at sea, none.

**The map uses Leaflet, with OpenStreetMap tiles in development.** Leaflet (through `react-leaflet`) is small, free, and has everything a trip map needs: markers, a route line, popups, and a clustering plugin. Tiles (the background map images) come from OpenStreetMap's own servers, which allow light use with attribution but not production traffic; deploying (M12) means switching to a provider, which is one URL. Rejected: MapLibre GL (GPU-drawn vector maps, smoother and styleable, but five times the size and more to learn than this milestone needs) and a keyed tile provider now (an account and a key before there's anything to deploy).

**Nearby photos are clustered.** Markers close together on screen merge into a numbered bubble that splits apart as you zoom in (Leaflet.markercluster), so 200 photos within 50 m of each other are one bubble, not a pile of overlapping pins. Rejected: one marker per M7 place (simple, but hides where in a city each photo was).

**GPX tracks come after the core map.** The map, route, scrubber, and stats come first; uploading a GPX track and placing no-GPS photos along it by timestamp is a follow-up.

**Background jobs are rows in a Postgres table, run by a worker we wrote.** Finishing an upload now only records that the file arrived and enqueues a job; a separate worker process does the slow part (download, decode, thumbnails, EXIF, time and place) and the browser polls for progress. The queue is a `jobs` table: a worker claims the next job with `SELECT ... FOR UPDATE SKIP LOCKED`, which locks the row it takes and skips rows other workers have locked, so several workers never take the same job and never wait on each other. Enqueueing happens in the same transaction as the change that needs the job, so a job can't be lost, and can't exist for a photo that was never saved; with a separate queue service those are two systems that can disagree (the "dual write" problem). The worker loop (claim, run, retry with backoff, reclaim jobs from a crashed worker) is hand-written, about 150 lines, so every part of it can be explained. Rejected: AWS SQS (managed and good AWS experience, but needs a transactional outbox to avoid the dual write, and an emulator or real AWS for local development and tests), Redis with RQ or Celery (a service to run, with the same dual-write problem), and the Procrastinate library (a Postgres task queue that does all this, but hides the parts this milestone is meant to teach).

**A crashed worker's job is taken over when its lease runs out, so every job must be safe to run twice.** Claiming a job sets a lease: `locked_until`, five minutes ahead, far longer than any job takes. If the worker dies, the job stays `running` until the lease expires, then any worker can claim it again; nothing is stuck, but a job can now run twice. So each one is idempotent: processing a photo that's already `ready` returns it unchanged, derivative keys depend only on the photo's id (a rerun overwrites, never duplicates), and an import item is only updated while it's still `processing`. A worker's result only counts if it still holds the job, so a slow worker whose job was taken over can't overwrite the new worker's outcome. Review found two gaps here, both fixed and tested: a duplicate whose job crashed after discarding the copy was rerun as "upload expired" (it's now recognised by its hash), and a worker that had lost its job could still mark the photo failed. Failures that aren't crashes (storage or database unreachable) are retried after 10s, then 20s, then the photo is marked failed with the error and shown with a Retry button. A file that can't be decoded isn't a job failure: as before, it's stored and flagged.

**Jobs are generic; photos keep their own status.** The `jobs` table holds any kind of work (processing a photo, regrouping trips, the orphan sweep) with its attempts, earliest run time, and lease. Photos keep the state the app shows: `pending → uploaded → processing → ready | failed`. Rejected: using the photo status as the queue (one less table, but regrouping and the sweep aren't about one photo).

**Trips are regrouped by one coalesced job.** Every finished photo asks for a regroup, but at most one regroup job waits at a time (a unique index allows only one queued `regroup_trips` row), so an import of 200 photos regroups a handful of times, not 200, and trips settle shortly after the last photo. It needs no notion of an import being "finished", so a file that never uploads can't hold it up. Rejected: regrouping once at the end of each import (exactly once, but an import whose last file never arrives would never regroup without a timeout).

**One image runs the API, the worker, migrations, and the place-data load (M10).** They're the same code started with different commands, so separate images would only add build time and a way for them to drift apart. The `Dockerfile` has three stages: Node builds the React app, Python installs the packages into a virtualenv, and the final stage copies in just the virtualenv, the code, and the built frontend. Node, npm's cache, and the compiler stay behind in the discarded stages. Test tools moved to `requirements-dev.txt`, so they don't ship. A `.dockerignore` keeps `.env`, `data/`, and local builds out of what Docker can copy: without it, `COPY` would bake the AWS keys into an image layer. In `compose.yaml` only the `api` service builds the image (`pull_policy: build`, so every `up` rebuilds it from the cache and never runs old code or pulls a same-named image from Docker Hub), and the other three use it with `pull_policy: never`: with `build:` on all four, compose built the same image four times, which filled the disk. Rejected: a separate nginx image for the frontend (see "FastAPI serves the built frontend").

**The final image is `python:3.13-slim-bookworm`, pinned by digest, with no compiler (M10).** It's Debian, so Python packages install as prebuilt wheels for both arm64 (Apple Silicon) and amd64. One exception surfaced in the first build: `timezonefinder` publishes Linux wheels only for x86_64, so on arm64 its small C extension has to be compiled. The middle stage installs `gcc` to build it, and only the compiled result is copied on. pip runs with `--only-binary=:all: --no-binary=timezonefinder`, so a new dependency that needs compiling fails the build instead of quietly depending on the compiler being there. The pin is a digest rather than just the tag, so a rebuild months from now gets the same bytes (each digest covers both architectures). Rejected: Alpine (its musl C library has fewer prebuilt wheels, so numpy, pillow-heif, and h3 could need compiling, and it has subtle DNS and performance differences) and distroless (the smallest attack surface, but its Python version is Debian's, and with no shell it's harder to debug).

**Containers run as an unprivileged user and say whether they're healthy (M10).** The image runs as user `app` (uid 10001). The code is owned by root and read-only to that user, so a compromised process can't rewrite the app. It's compiled to bytecode at build time, since the app can't write `__pycache__` later. The API's healthcheck requests `/healthz` with Python's own `urllib` (slim has no curl). The worker serves no HTTP, so it touches a heartbeat file each time it successfully checks the queue, and its healthcheck fails if the file is over a minute old. A hung worker or one cut off from the database turns unhealthy, while one that's merely idle doesn't. Commands use the exec form, so the process is PID 1 and gets `docker stop`'s SIGTERM itself, and the worker has 30 seconds to finish its jobs before it's killed.

**Migrations run in a one-shot `migrate` service (M10).** It runs `alembic upgrade head` and exits. The API and worker wait for it with `depends_on: condition: service_completed_successfully`, so they never run against an old schema, a failed migration shows as its own failed container, and it runs once however many API containers start. M12's deploy can run it the same way, as a one-off task before the new version starts. Rejected: migrating in the API's startup (two replicas would race, since Alembic takes no lock, and the worker could start first).

**A fresh clone stores photos in SeaweedFS; the containers follow whatever `.env` says (M10).** Someone trying the app shouldn't need an AWS account, so `.env.example` points at the local SeaweedFS. Inside the compose network, services reach each other by service name: in a container, `localhost` is that container itself. So the API reaches storage at `DOCKER_S3_ENDPOINT` (`http://objects:8333`), while browsers get URLs signed for `http://localhost:8333` (`S3_PUBLIC_ENDPOINT`, falling back to `S3_ENDPOINT`). That's the M5 two-endpoint split. With both endpoints empty, the containers use Amazon S3, as my own `.env` does. That matters because my worktrees share one compose project, so a full `docker compose up` uses the real dev database, whose photos are in S3. Compose builds `DATABASE_URL` itself, with the host `db`, from the `POSTGRES_*` values, and stops with a message (`${VAR:?}`) if `.env` leaves one out, rather than starting with an empty password. Published ports bind to 127.0.0.1, so the database and storage aren't reachable from other machines on the network. In `.env.example` each secret appears once and the other settings refer to it (`S3_ACCESS_KEY=${SEAWEEDFS_ACCESS_KEY}`, the password inside `DATABASE_URL`). Both compose and python-dotenv expand `${...}` in `.env`. The rehearsal caught this: with each `change-me` replaced separately, the app's storage keys would no longer match SeaweedFS's.

**Place data loads by itself on first start, without holding anything up (M10).** A one-shot `geodata` service runs `python -m app.geodata load --if-empty` after migrations. It exits at once if place data is already loaded, and otherwise downloads (cached in a volume) and loads it. Nothing waits for it: the app works meanwhile, and photos get place names when the load finishes, since a load re-matches every photo. Rejected: a documented command only (simpler, but "clone and `docker compose up`" would show a map with no place names).

**Development stays outside Docker; the full stack is for production-like runs (M10).** The API, worker, and Vite keep running on the Mac with only `db` and `objects` in Docker, because reloading on every save through bind mounts and Colima's VM is slow and unreliable, and `node_modules` built for macOS doesn't work in Linux. The API and worker containers have `restart: on-failure`: an early worker crashed on a failed lookup of `db` during startup, and this restarts after a crash like that. Rejected: `unless-stopped`, which also restarts containers whenever Docker starts, so after every wake of the Mac the full stack would come back and take port 8000 from the API run on the Mac. The containerized worker uses the dev database, never `trips_test`, so it can't take the tests' jobs.

**"A fresh machine can clone and run it" is tested with a smoke script, not by eye (M10).** `scripts/smoke_compose.py` checks a running stack the way a browser uses it: the React app is served, the container runs as non-root with no compiler, an import's file goes to the presigned URL (proving the URL is signed for a host reachable from outside Docker), the worker container processes it, and the thumbnail redirect serves a WebP. It uses only Python's standard library, so it needs nothing on the host but Docker and Python, and it can become M12's CI job. It refuses to run against a stack that already has photos, since there's no delete. The rehearsal is a copy of exactly what a clone would contain, in a temporary folder, with its own compose project, a `.env` made from `.env.example`, and a build with no cache. It passed on 2026-10-03: the uncached build took 85 seconds, everything was healthy 11 seconds later, and a second start skipped the migrations and place data it had already done. Then the real test: someone else's laptop.

**Still no Redis (M10).** The guide's compose file lists Redis, but background jobs are a Postgres table (see "Background jobs are rows in a Postgres table"), so nothing would use it.

**Sign in with Google, then our own session (M11).** Google proves who someone is; after that the app keeps its own session: a random 256-bit token in a cookie, with only its SHA-256 stored in a `sessions` table. Signing out deletes the row, so it ends at once. The cookie is `HttpOnly` (page scripts, including injected ones, can't read it) and `SameSite=Lax`. The Google step is the OpenID Connect authorization code flow, written by hand with `httpx` (about 80 lines, no new dependency): a `state` value in a short-lived cookie ties Google's answer to the browser that started it, a `nonce` ties the ID token to this sign-in, and PKCE means an intercepted code is useless on its own. The ID token's signature isn't checked, because it comes straight from Google's token endpoint over HTTPS in answer to our request, which the spec accepts instead (OpenID Connect Core, 3.1.3.7); its issuer, audience, expiry, nonce, and `email_verified` are. Rejected: JWTs as the session (can't be revoked before they expire without a deny-list, which is a session table again), a password of our own (storing password hashes and handling resets, for no gain when everyone has Google), and a managed service like Cognito or Auth0 (least code but least to explain, and tests would have to fake it).

**Accounts are invite-only (M11).** There's no sign-up page: `python -m app.users create <email>` makes an account, and Google sign-in only matches an existing one, by verified email. Once deployed, a stranger can't sign up and fill the S3 bucket, and the upload path isn't open to the internet. For setups without Google (a fresh clone, the smoke test), the same command prints a one-time sign-in link: valid 15 minutes, stored hashed, spent on use. The link opens a page with a button rather than signing in on GET, because mail scanners and chat previews fetch links, and would use it up. Rejected: open sign-up (needs quotas, rate limits, and abuse handling first), and an allow-list behind a sign-up page (the same protection with more code).

**One user can never see another's data, enforced three ways (M11).** Every table with user data has a `user_id`, and every query filters by the signed-in user, passed explicitly from the route down (there's no global "current user"). Another user's photo or trip is a 404, the same as one that doesn't exist, so trying ids reveals nothing. Behind that, the database itself refuses some mix-ups: a photo's trip is a foreign key on `(trip_id, user_id)`, so a photo can't be put in another user's trip even by a buggy query. And tests: one walks every API route and checks each refuses a request with no session (so a new route can't forget), and others have user B try to read and change each of user A's things. Breaking the owner check in `get_photo` or `_require_trips` makes them fail. Duplicate detection is per user too: a shared one would tell B "already imported", revealing that A has the photo.

**Each user's writes are serialized separately (M11).** Writes take a Postgres advisory lock because they end by recomputing times and trips from all of a user's photos. That lock is now per user (shared global key, plus an exclusive one for the user), so one person's import doesn't wait for another's. Reloading place data, which touches everyone's photos, takes the global key exclusively.

**Cross-site requests are refused twice over (M11).** `SameSite=Lax` keeps the cookie off POSTs started by other sites, and a middleware also refuses any change (POST, PUT, PATCH, DELETE) whose `Origin` header isn't one of `APP_ORIGINS`. Browsers always send `Origin` on those, and a page can't fake it. The second check covers what SameSite doesn't: sibling subdomains count as the same site, and some older browsers ignore it.

**A share is an unguessable link to a view built for strangers, not the owner's page with bits hidden (M11).** `/s/<token>` shows one trip, read-only, to anyone with the link. The token is 192 random bits (not an id, so it can't be guessed or counted up to), and a share can be revoked: a revoked link and one that never existed both answer 404. The public view is built in `app/shares.py` from an allow-list of fields, so a column added to photos later doesn't leak by default: positions, place and country names, day and time, and whether there's an image. No filenames, cameras, EXIF, altitude, or exact coordinates. Images are served only as the derivatives, which are re-encoded without metadata, never the original. A test downloads the files a share serves and checks their raw bytes for EXIF, GPS, and XMP. Tokens are stored as they are (unlike session tokens) so the owner can copy a link again later; someone who can read the database can already see everything a share shows. The JSON is `no-store` and `noindex`. Browsers send only the site's origin, not the page URL, to other sites (the map tiles, the image store), so the token doesn't leak through `Referer`; the stricter `no-referrer` would break OpenStreetMap's tiles, which require a referrer. Rejected: sequential or trip-id URLs (guessable), and links that can't be revoked.

**Locations are snapped, and coarse by default (M11).** A share's precision is chosen when it's created. `city`, the default, puts each photo at its town's point, found with the same rule as a photo's place (deepest inside a radius that grows with population) but skipping GeoNames' sections of cities, so a photo in Lisbon's Baixa shows as Lisbon, not Baixa. `neighbourhood` puts it at the centre of the H3 hexagon it's in (resolution 8, about 0.9 km across), named by its place. A photo with no town within 50 km gets no position at all rather than its real one. Snapping beats random noise: noise averages out, so fifty jittered photos taken in one flat still centre on it, while snapped ones all land on the same point. Rejected: exact positions with an option to coarsen (the guide's rule is to default to coarse, since most people never change a default), and a random fuzz radius (above).

**Home is detected, confirmed by you, and never shared (M11).** Photos within any home's radius are left out of every share, whatever trip they're in (a trip can include home photos if you move them in by hand). Since a share is only as safe as the homes you've set, the app suggests homes: photos are binned into ~0.9 km hexagons, each scored with its neighbours (about 3 km across), and a spot with photos on at least 5 different days spread over at least 2 weeks is suggested. A trip has many photos in one place too, but on a few consecutive days, so it doesn't qualify. Suggestions are only ever shown to you, and nothing changes until you add one: a wrong automatic home would silently regroup your trips. The share dialog warns when no home is set. Rejected: adding detected homes automatically (above), and excluding home only when you remember to set it (no suggestions).

**Shares show a month and day numbers, and never the last week (M11).** By default a share says "July 2025" and "day 2, 14:30", not dates: exact dates reveal routines (the same week every year) and line up with anything else you post. The owner can choose exact dates per share. A trip can be shared only 7 days after its last photo, and photos from the last 7 days are left out of every share even if added to an older trip later, so a link never says "away right now", which is "the house is empty". Rejected: a delay alone (exact dates still show routines), and blurring dates at random (the same averaging problem as locations).

**CI runs everything on every change, and only `master` publishes an image (M12).** GitHub Actions runs three jobs in parallel on every pull request: the backend (ruff, then pytest against the same Postgres + PostGIS and SeaweedFS that `compose.yaml` starts for development, so CI can't drift from it), the frontend (lint, typecheck and build, tests), and the image (build it, start the whole stack, and run M10's smoke script). On a push to `master`, once all three pass, a fourth job pushes the image to GitHub's registry (GHCR), tagged with the commit SHA and never `latest`: a deploy names exactly what it runs, and rolling back is deploying an older SHA. The repo is private, so its image storage is capped at 500 MB, and the publish job keeps only the newest five. CI's `.env` is made from `.env.example` with random secrets (`scripts/ci_env.sh`), so it needs no stored secrets at all. Python lint is ruff with its default rules, the ones that catch bugs (unused imports, undefined names), not style. The tests ignore any Google client in `.env`, so they behave the same on every machine.

**Production is one DigitalOcean droplet running the published image, behind Caddy (M12).** `deploy/compose.yaml` runs Caddy, the API, the worker, Postgres, and the one-shot migrate and geodata services on a 2 GB droplet. Caddy is the only thing reachable from outside (ports 80 and 443): it gets and renews the Let's Encrypt certificate by itself and redirects HTTP to HTTPS, and it adds HSTS, `nosniff`, a frame ban, and the referrer policy. The API isn't published, so it can trust the forwarded `https` scheme from any proxy (`FORWARDED_ALLOW_IPS=*`): that's what makes the app see `https://` and mark the session cookie `Secure`. The server never builds anything: it runs the image CI published, named by commit SHA in `.env`, and `deploy/deploy.sh <sha>` pulls it first, so a wrong tag fails before anything stops. The database is the official `postgis/postgis` image, pinned by digest; development builds its own only because that image has no arm64 build. The address is a free DuckDNS name. The droplet is in New York, near the bucket in us-east-2, because the worker downloads every original from it. Rejected: managed Postgres (about $15 a month more, more than the droplet itself, and its extensions are up to the provider), building on the server (slow, and what runs wouldn't be what CI tested), and nginx with certbot (two moving parts for what Caddy does alone). The step-by-step notes are in [docs/deploy.md](docs/deploy.md).

**Backups: a nightly `pg_dump` to the bucket, and a restore that has been run (M12).** `deploy/backup.sh` dumps the database in Postgres' custom format and `python -m app.backup` streams it to `backups/` in the photo bucket, using the app's own credentials, so it needs nothing else installed. Place data isn't in the dump (171,000 rows that can always be downloaded again): after a restore, `geodata` finds the place tables empty and reloads them. Backups older than 30 days are pruned, but the newest seven are always kept, so if backups silently stop for a month the last good ones survive. Upload refuses anything that doesn't start like a real dump, so a failed `pg_dump` can't replace a backup with an error message. Writing this turned up a bug before it happened: the orphan sweep deletes any object no photo refers to, which would have deleted every backup within the hour; `backups/` is now excluded, with a test. A restore was rehearsed: dump, restore into an empty database, compare row counts.

## Performance baseline (M3, synchronous processing)

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

## Known gaps

- Backups are in the same bucket, written with the same credentials as the app. Someone who takes over the app could delete them too, and there's no copy outside this AWS account. A separate backup user that can only add objects, with versioning on the bucket, would fix both.
- Production is one server: if the droplet is down, so is the site. Postgres on it means a restore loses up to a day of changes (nightly backups).
- The server is administered as root over SSH (keys only). A deploy user with limited sudo would be better.
- M10 isn't done until the stack has been started from a fresh clone on someone else's laptop. So far it has only run on my own Mac (arm64): the amd64 build, which is what an Intel laptop or a CI runner would make, hasn't been tried.
- The image is 592 MB (154 MB compressed), mostly numpy, Pillow, pillow-heif, and boto3. Nothing has been done to trim it.
- The first start needs internet access for the place data. Offline, `geodata` fails and photos have no place names until it's run again.
- A reload during an import stops its uploads: the files were only in the page's memory. The import resumes by picking the same files again, matched by hash.
- M3 has only been tested with synthetic photos. Its "done when" still needs a real trip across a timezone change, in particular to confirm that real iPhone HEIC portraits come out upright (pillow-heif may already apply rotation, and rotating twice would be wrong).
- The worker downloads each original into memory to process it (as `complete` did before M8). Fine for photos up to the 200 MB limit, but four threads processing large files at once need several hundred MB.
- There are no upload quotas: an invited user can upload as much as they like. The 200 MB per-file cap limits a single file, not the total.
- A revoked share stops working at once, but an image URL it already handed out keeps working until it expires (up to an hour: presigned URLs are signed for an hour).
- A share still shows times of day and place names, which together can hint at routines. Day numbers keep exact dates out, but not the season.
- Share views aren't rate-limited. Guessing a 192-bit token isn't feasible, but nothing slows down a script that tries.
- Sessions last 30 days and there's no "sign out everywhere" or list of active sessions yet; deleting a user's rows from `sessions` does it.
- `POST /api/photos` rejects files over 200 MB before reading them into memory, but the web framework has already saved the whole request to a temporary file on disk by then, so a huge upload still costs disk space. Presigned uploads don't have this problem, since the file never reaches the API.
- Uploads are limited to `image/*` (except SVG, which can contain script) and `application/octet-stream`, because the stored type is served back to browsers. Photos uploaded before this rule keep whatever type they were given.
- The browser reads each file fully into memory to hash it (Web Crypto has no streaming hash). Fine for photos, but a 200 MB file briefly costs 200 MB of browser memory. Hashing also needs a secure context (HTTPS or localhost).
- Import items that are never uploaded stay `uploading` forever (their pending photo is swept after an hour). The import history shows them as "not uploaded", and they can be resumed by picking the files again.
- SeaweedFS allows cross-origin requests from any site by default, while real S3 denies them unless the bucket's CORS rule allows the origin. The app sets an explicit rule on SeaweedFS too, but the permissive default means offline development and tests can't catch a mistake in it. The S3 bucket's rule is set by hand in the console until M12 moves it to Terraform, so it can drift from `APP_ORIGINS`.
- RAW files and video aren't decoded: they're stored, with no metadata or previews.
- A wall-clock time in the repeated hour when clocks fall back is ambiguous. We take the first occurrence. Only a recorded offset or GPS time can disambiguate it.
- Some cameras write a stale GPS timestamp (from the last fix, not the shot), so `gps` is trusted below the recorded offset.
- Neighbour matching doesn't know about trips. A no-GPS photo can borrow a zone from a photo on a different trip if one is within 48 hours. Scoping it would create a circular dependency (trips need times, which need zones, which would need trips), so it's left as is.
- Trips can't be unlocked, so a locked trip never benefits from later algorithm improvements. An "unlock" action would fix this.
- Home suggestions need photos taken at home on at least 5 days over 2 weeks. Someone who rarely takes photos at home gets no suggestion, and their shares are only as safe as the homes they set by hand.
- Photos without GPS have no place, even when a phone photo minutes later does. Times already borrow a zone from a nearby located photo; places could borrow the same way, but a guess would need marking as one, like a guessed zone.
- Place names come from GeoNames' primary name, usually the local one ("Lisboa" would be ideal for some, "Lisbon" for others). Its alternate names include translations, but they aren't loaded.
- Map tiles come from OpenStreetMap's servers, whose usage policy doesn't cover production traffic. Deploying (M12) needs a tile provider; it's the `TILES` URL in `frontend/src/map/TripMap.tsx`.
- The route draws straight lines between photos, so its distance is a lower bound for a winding road and too high for noisy GPS: photos jumping back and forth add distance never travelled. A GPX track (the planned follow-up) gives the real path.
- No GPX import yet, so photos without GPS (the Fujifilm's) aren't on the map, even when their time puts them between two located photos.
- Elevation is shown only when photos carry GPS altitude, and climb counts only rises between photos (5 m or more), so it's a lower bound.
- The place data is a snapshot: `python -m app.geodata load` refreshes it and re-matches every photo, but nothing schedules that.
- A photo far out at sea gets its nearest place however distant (stored with its distance), and no country.
- The grouping thresholds (48h, 300 km / 24h, 3 photos, 25 km) are constants, not settings.
- The timeline page only merges a trip with the next one. The API merges any set of trips.
- Trip bounding boxes don't handle the antimeridian: a trip across the Pacific date line (e.g. Fiji) gets a box spanning the whole globe.
- Regrouping is O(n) per write across all photos. Trips have the same scaling note as time resolution.
- Clock offsets match the camera exactly on make, model, and serial. The same model without a serial number is treated as a different camera.
