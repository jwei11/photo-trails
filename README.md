# Trip Mapper

Drop in the photos from a trip. Trip Mapper reads the timestamps and GPS out of them, works out where you went and when, groups them into trips, and puts each trip on a map you can share without giving away where you live.

**Live:** https://tripmapper-jw.duckdns.org — press **Try the demo**: no account needed. You get your own copy of a real trip (Death Valley and Sequoia) to explore, edit, and share for 24 hours.

## What's interesting about it

- **Time is harder than it looks.** Cameras record a wall-clock time with no timezone. Each photo's instant and zone come from a fallback chain (recorded offset → GPS time → timezone-at-coordinates → nearest located photo → default), and the source is recorded. Corrections for wrong camera clocks are reversible. → [how-it-works.md](docs/how-it-works.md)
- **Uploads skip the API.** The browser hashes each file (SHA-256, so duplicates are never uploaded), then PUTs it straight to S3 with a presigned URL. The API only queues a job. A `complete` call went from **812 ms to 52 ms** (p50).
- **A job queue in Postgres, written by hand.** Workers claim jobs with `SELECT … FOR UPDATE SKIP LOCKED`, with leases, retries with backoff, and idempotent jobs. Jobs are enqueued in the same transaction as the change that needs them, so there's no dual-write problem. Regrouping after 200 uploads coalesces into **6 runs instead of 200**.
- **PostGIS for places.** 171k places loaded with `COPY` (250× faster than row inserts); a nearest-place lookup through the GiST index is **~800× faster** than a full scan.
- **Sharing that's private by default.** Share links are 192-bit tokens and can be revoked. A shared trip snaps locations to the city, hides your home (detected from your photo history, and confirmed by you), shows "day 2" instead of dates, and never includes the last 7 days. Images are re-encoded with no EXIF; a test checks the bytes.
- **A public demo on a $6 server.** Each visitor gets a throwaway account holding a copy of the demo trip's database rows that points at the same stored images, so a new visitor costs about 40 rows, not 240 MB of photos. Accounts that weren't invited have quotas, capped numbers, a per-address limit (kept as a keyed hash, never the IP), and their background jobs run after the owner's. → [decisions.md](docs/decisions.md)
- **Multi-user isolation enforced three ways:** every query scoped by user, composite foreign keys, and a test that walks every route.
- **Shipped.** Docker image built in CI (GitHub Actions: lint, tests, and a full-stack smoke test), published to GHCR by commit SHA, and run on a $6 DigitalOcean droplet behind Caddy (automatic HTTPS), with nightly `pg_dump` backups to S3 and a restore that has been tested.

## Stack

| | |
|---|---|
| Backend | Python 3.13, FastAPI, SQLAlchemy Core (raw SQL, no ORM), Alembic |
| Data | PostgreSQL 17 + PostGIS, S3 (SeaweedFS locally) |
| Frontend | React + TypeScript (Vite), TanStack Query, React Router, Leaflet |
| Ops | Docker Compose, GitHub Actions, GHCR, DigitalOcean, Caddy |
| Tests | pytest (~190), Vitest + React Testing Library (~75) |

```
 browser ──HTTPS──▶ Caddy ──▶ api (FastAPI + built React app) ──▶ Postgres + PostGIS
    │                                                                 ▲   (jobs table)
    └── presigned PUT/GET ──▶ S3 ◀──────────────── worker ────────────┘
```

## Run it locally

Needs only Docker.

```bash
cp .env.example .env     # replace every change-me (letters and digits only)
docker compose up --build
docker compose exec api python -m app.users create you@example.com --login-link
```

Open the printed link to sign in. Full development setup (hot reload, tests, S3, Google sign-in): [docs/development.md](docs/development.md).

## More detail

| | |
|---|---|
| [docs/how-it-works.md](docs/how-it-works.md) | How photos get their time, and how they become trips |
| [docs/decisions.md](docs/decisions.md) | Every significant design decision, with the alternatives rejected and why |
| [docs/performance.md](docs/performance.md) | Benchmarks for each milestone, before and after |
| [docs/known-gaps.md](docs/known-gaps.md) | What's missing or wrong, stated plainly |
| [docs/deploy.md](docs/deploy.md) | Production deploy runbook |
| [docs/development.md](docs/development.md) | Local setup, tests, linting |
| [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md) | The milestone plan the project followed |
