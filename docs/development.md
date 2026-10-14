# Running and developing Trip Mapper

## Run it (Docker only)

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

## Develop

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
