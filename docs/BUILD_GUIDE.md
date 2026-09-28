# Trip Mapper — build guide

Drop in the photos from a trip. It reads the timestamps and GPS out of them, works out where you went and when, puts it on a map, and gives you something worth sharing — including a clean list of the places you'd actually recommend.

Supersedes the earlier photo-pipeline guide. Same infrastructure spine, better product on top of it.

## What this is and why it's worth building

The old framing was "a photo ingest pipeline." Technically fine, but nobody needs another one, and you could feel that.

The new framing: your photos already contain a trip log, and nothing surfaces it. Every photo has a timestamp and most have coordinates. That's a route, a timeline, and a list of places, sitting unused in files you already have. The product is the thing that reads it back out.

That single change fixes three problems at once:

* It's actually useful. Recovering "where did we eat on day three of that trip" from photos is a real thing people want and can't easily do.
* It's shareable. A trip map with your recommendations on it is something you send to a friend planning the same trip. Shareable means someone other than a recruiter will see it.
* The hard parts got more interesting. Trip segmentation, reverse geocoding against a world dataset, and privacy-preserving sharing are all better interview material than "I generate thumbnails."

What has not changed: the infrastructure is still the part that gets you hired. Deployed, monitored, reproducible, with real failure stories. The features make you finish it; the infrastructure makes it count.

## How to use this document

Each milestone has the same shape:

* Goal — one sentence.
* Build — what to make.
* You will hit — the problem this milestone exists to teach. If you don't hit it, you probably skipped something.
* Done when — how you know to move on.
* For the interview — what this gives you to talk about.

There is deliberately no code here. Struggle is the curriculum. When stuck: read the error, read the official docs, then ask about the concept rather than the solution.

Three milestones build the wrong thing first on purpose (M1→M8, M5→M6, M12 manual→Terraform). Don't skip ahead. The contrast is where your best answers come from.

## Phase 1 — Get photos in and read them (M0–M6)

### M0 — Decide and set up

Goal: an empty repo with a hello-world endpoint on localhost.

Build

* Pick your language. Python/FastAPI, Node/Express, or Go — whichever you debug fastest. The ops story matters far more than the stack.
* `git init`, README, `.gitignore` (venv, caches, `.env`, `uploads/`).
* One endpoint returning `{"status": "ok"}`. Name it `/healthz` — Docker and your monitoring will both want it later.
* Editor, linter, formatter configured now so they're never a distraction.

Done when you can clone your own repo to a fresh folder and run it.

### M1 — The naive version

Goal: upload a photo, list your photos. Wrong architecture, working code.

Build

* `POST /photos` — accepts a file, writes to a local `uploads/` folder, returns an ID.
* `GET /photos` — lists them.
* `GET /photos/{id}` — returns the file.
* Metadata in a dict or JSON file. No database yet.

You will hit the ID question (sequential counters collide across processes and leak how many photos exist), and the filename question (client-supplied names collide and can escape your directory).

Done when you've uploaded ten of your own photos and can list them.

For the interview: this is your baseline. Everything after is a fix to something here.

### M2 — Postgres

Goal: metadata in a real database with real migrations.

Build

* Postgres locally. Use the PostGIS image from the start — you need the extension at M7 and swapping the image later means recreating the volume.
* A `photos` table. Files still on local disk.
* A migration tool. Write migration #1 as a migration, not a hand-run CREATE TABLE.

You will hit the schema question: real columns vs. a JSONB blob. Rule of thumb — if you filter, sort, or join on it, it's a column. Otherwise JSONB. Write the reasoning in your README.

Done when you can drop the database, re-run migrations, and be back to a working schema.

### M3 — The photo dater

Goal: the core read. Every photo gets a when and a where.

This is the heart of the product. Everything downstream is built on what you extract here.

Build

* Parse EXIF: `DateTimeOriginal`, GPS coordinates, camera, lens, exposure.
* Store the full tag set in JSONB; promote `shot_at`, `lat`, `lon` to columns.
* Handle photos with no GPS (older cameras, disabled location) and photos with no timestamp (screenshots, downloads). Both are common. Decide what the app does with them rather than crashing.
* A timeline view: your photos in chronological order.
* Generate a thumbnail and a web-size version.

You will hit — and this is the milestone's real content —

1. Timezones, which are genuinely hard here. `DateTimeOriginal` is local time with no zone attached. GPS timestamps are UTC. A photo shot at 2pm in Lisbon and one shot at 2pm in Denver are eight hours apart and look identical. You cannot correctly order a multi-country trip without solving this. The usual approach: derive the zone from the coordinates, then convert. Photos with no GPS have to inherit a zone from their neighbours.
2. Camera clock drift. Clocks are set wrong, left on the wrong zone after a flight, or off by hours. You need an offset correction — per-camera, per-trip. Anyone who has actually merged two photographers' cards knows this pain.
3. Orientation. Phones store rotation as a tag. Strip metadata naively and portraits come out sideways.
4. Speed. Time a 40MB upload. Then two at once. Write the numbers down — you need the before figure for M8.

Done when you can drop a real trip's photos in and get a correct chronological timeline across at least one timezone change.

For the interview: timezone handling is a topic senior engineers respect because they've been burned by it. Have a specific example ready.

### M4 — Trips

Goal: photos stop being a flat list and become trips.

Build

* An algorithm that groups photos into trips automatically.
* A `trips` table; photos belong to a trip.
* Let the user merge, split, and rename trips — your algorithm will be wrong sometimes and the fix should be a click, not a re-run.
* Per-trip stats: dates, duration, number of photos, distance covered, bounding box.

You will hit the fact that there is no correct answer, which is what makes this good. Start simple: a gap of more than N hours between photos starts a new trip. Then find the cases that breaks:

* A weekend at home between two legs of one trip
* A day trip an hour from your house — is that a trip?
* Photos with no timestamp, which have no position in the sequence
* One photo, alone, in the middle of nowhere
* Two trips that overlap because you imported a friend's photos

Then add distance: a jump of several hundred kilometres starts a new trip even without a time gap. Then think about whether "home" should be excluded entirely — which requires knowing where home is, and that's M11's problem.

Done when you can import a year of your own photos and the trips it finds are mostly right.

For the interview: the strongest algorithmic story in the project. "How did you define a trip, what did you try first, why did it fail, what do you do when the algorithm is wrong" — a real design conversation with no textbook answer.

### M5 — Object storage

Goal: files leave local disk.

Build in two passes. Do not skip the first.

Pass A: MinIO locally (S3-compatible). Uploads still go through your API, which forwards to storage. Get it working.

Pass B: presigned URLs. Client asks your API for a URL, uploads straight to storage, then tells your API it's done. Three requests instead of one.

You will hit

* Why pass B exists. In pass A your API holds the file for the whole transfer. Ten concurrent uploads and it serves nobody.
* The endpoint signing problem. A presigned URL bakes the storage hostname into its signature. Sign against a container-internal hostname and the URL is useless from your laptop, which can't resolve it. This will confuse you for an hour. The fix is two configured endpoints: one for signing, one for transfers.
* Orphans. A client can request a URL and never upload, leaving rows pointing at nothing.

Done when your API logs show two tiny requests per upload, not one large one.

### M6 — Bulk import

Goal: a whole trip at once, not one photo at a time.

Build

* Multi-file upload with per-file progress.
* Deduplication — importing the same card twice should not double everything. Hash the file contents and check before storing.
* Partial failure handling: 200 photos, three fail. The other 197 must succeed, and you must be able to see and retry the three.

You will hit the realisation that this is the actual use case. Nobody uploads one photo from a trip; they dump a card. Everything you built assuming single uploads gets stress-tested here.

Done when you can drop 200 photos in one go and it holds together.

## Phase 2 — Make it a map (M7–M9)

### M7 — Geography

Goal: coordinates become places. This is the world atlas piece.

Build

* Enable PostGIS. Store photo locations as real geometry with a spatial index.
* Load a geographic dataset — this is the atlas:
   * GeoNames (`cities1000` / `cities15000`) — a few MB, millions of place names with coordinates, population, country, admin region. Free, offline.
   * Natural Earth — country, state, and region polygons for boundaries.
* Reverse geocode by nearest-neighbour query: given a photo's coordinates, find the nearest named place. Then the containing country and region.
* Derive trip summaries from it: countries visited, cities, "5 days in Portugal, 2 in Spain."

You will hit

* This is a real ETL job. Download, parse a tab-separated file with millions of rows, load into Postgres, index it. Your first attempt will be slow. Batch inserts and `COPY` exist for a reason.
* Nearest-neighbour needs the right index. A naive distance calculation over millions of rows is unusably slow. PostGIS has an operator that uses the spatial index for ordering; find it and understand why it's different.
* Nearest isn't always right. The nearest named place to a beach might be a village nobody has heard of. Population-weighting helps. So does knowing which admin boundary you're inside.
* The API alternative and why you're not using it. Nominatim, Mapbox, and Google all reverse-geocode. All rate-limit, need keys, and cost money at volume. Doing it locally is faster, free, and teaches you more. Worth building an API path as a fallback for detail you don't have — and then you'll need caching, which is its own lesson.

Done when a photo's coordinates reliably resolve to a place name, and a trip tells you which countries and cities it covered.

For the interview: PostGIS and spatial indexing is a genuinely differentiating skill. Almost no sophomore has touched it.

### M8 — Async processing

Goal: the API returns immediately; a worker does the slow work.

The hardest conceptual jump in the project. Budget two weeks.

By now the per-photo work is real: decode, thumbnail, EXIF, timezone resolution, reverse geocode, dedup hash. Times 200 photos in a bulk import. It cannot happen inside a request.

Build

* A queue (Redis, or Postgres-as-a-queue — pick one and know why).
* A separate worker process.
* A status field per photo: `pending → uploaded → processing → ready | failed`. Draw the state machine before writing it.
* Import-level progress: "173 of 200 done, 2 failed."
* The client polls for status.

You will hit

* Failures happen where nobody is looking. Worker crashes mid-job — what does the row say? Make failure visible, record the error.
* Idempotency. If a job runs twice, do you get two thumbnails or one overwritten? Make re-running safe, and retries become free.
* Ordering. Trip segmentation needs all the photos before it can group them. So per-photo work is parallel, but trip assignment has to wait. That's a dependency between jobs, and it's a real distributed-systems problem in miniature.

Done when a 200-photo import returns instantly and you can watch progress climb. Compare to your M3 numbers.

For the interview: the single strongest story in the project. Have the before/after numbers.

### M9 — The map

Goal: something worth looking at.

Build

* A trip map: photo locations, route between them in time order, clickable markers showing the photo.
* A timeline scrubber — drag through the trip and watch it unfold.
* Trip stats displayed: distance, duration, places, elevation if you have it.
* Optional GPX upload: if you tracked the hike, align photos to the track by timestamp. Photos without GPS can be placed by interpolating between track points, which is a nice trick and only possible because M3 got time right.

You will hit map libraries (Leaflet is the easy free option) and the question of what to do when 200 photos are all within 50 metres of each other. Clustering markers is the standard answer.

Done when you'd voluntarily show someone one of your own trips on it.

## Phase 3 — Infrastructure (M10–M13)

This is the part that gets you hired. Do not let the semester run out before it.

### M10 — Containers

Goal: the whole stack runs from a clean clone with one command.

Build

* Multi-stage Dockerfile; final image carries no compiler. Non-root user. Healthcheck.
* Compose file: API, worker, Postgres/PostGIS, Redis, MinIO.
* `.env.example` with no real secrets.

You will hit container networking — services address each other by service name, not `localhost`. This is where the M5 endpoint problem finally makes sense.

Done when a fresh machine can clone and `docker compose up` successfully. Actually test this on someone else's laptop.

### M11 — Auth, sharing, and privacy

Goal: other people can see your trips. Safely.

This milestone is where the product becomes real, and it is also where you have to take privacy seriously for the first time. Do not treat it as a checkbox.

Build

* Accounts and login. Photos and trips belong to users.
* Public share links: a read-only trip page, reachable without an account.
* A test proving user A cannot read user B's photos. Write it. Reviewers notice.

The privacy design, which is the actual content here:

* Your photos contain your home address. A cluster of hundreds of photos at one location over months is where you live. Detect it, and exclude it from trips and shares by default. This is not hypothetical — it's the single biggest risk in the app.
* Exact coordinates are too precise to publish. A shared trip should snap locations to the nearest named place, or fuzz them to a radius, rather than publishing the metre-accurate position of your friend's apartment. Make the precision a choice, and default it to coarse.
* Strip EXIF from every derivative. Have a test asserting it. A shared JPEG with GPS intact defeats everything above.
* Timing matters too. A public trip that says you're currently away is a statement that your house is empty. Consider delaying or blurring dates on public shares.
* Share links should be revocable, and unguessable (random tokens, not sequential IDs).

You will hit the tension between "useful to share" and "safe to share." There's no setting that's right for everyone, which is why the defaults matter so much. Write down what you chose and why.

Done when you can send a friend a link to a real trip and you're comfortable with everything it reveals.

For the interview: the strongest non-infrastructure topic in the project. "How did you handle location privacy" gets a specific, thought-through answer with concrete threats and concrete mitigations. Very few candidates at any level can do this well.

### M12 — CI and deploy

Goal: a real URL, over HTTPS, that someone else can open.

Build — CI first

* Tests worth running. Keep logic in pure functions (timezone resolution, trip segmentation, distance maths) so they test without a database and CI stays fast.
* GitHub Actions: lint + test on every PR. Branch protection on `main`.
* On merge: build image, push to a registry, tag with the commit SHA, not `latest`.

Then deploy, in two passes.

Pass A — by hand. A small VM ($5–12/month on DigitalOcean, Hetzner, or Fly.io; check the GitHub Student Developer Pack for credits first). SSH in, install Docker, reverse proxy, domain, TLS certificate. Take notes on every step.

Pass B — as code. Terraform: VM, firewall, database, storage bucket, DNS. Remote state. Then destroy everything and rebuild it from scratch. If it comes back, your infrastructure is real code. If not, you have hidden manual steps to find.

Then automate: merge to `main` ships.

Cost discipline: billing alarm before any resource exists. AWS changed its free tier in July 2025 — new accounts get credits with a six-month ceiling rather than twelve months of free usage, and the account stops when it expires. That's a bad fit for a project you want live during spring interviews. Use a cheap VM as the real deployment; do an AWS version afterward as a deliberate Terraform exercise, screenshot it, then tear it down.

Done when you can send someone a link, and separately when `terraform destroy && terraform apply` gets you back to working.

For the interview: "my environment is entirely in Terraform; I tear it down and rebuild in minutes" is the strongest single line in the project.

### M13 — Observability and failure

Goal: you find out it's broken before your users do, and you have evidence.

Build

* Structured JSON logs with a request ID threaded through API and worker, so you can follow one photo end to end.
* Metrics: request rate, latency, error rate, queue depth, job duration, import success rate.
* A Grafana dashboard you'd actually look at.
* One alert that reaches your phone. Queue depth growing without bound is a good first choice — it means the worker died.
* `/healthz` (liveness) and `/readyz` (readiness). Know why both exist.

Then break it on purpose.

* A k6 load test. Find where latency degrades. Fix it. Record before/after in the README.
* Kill the database mid-request. Kill the worker mid-import. Fill the disk. Deploy a broken config.
* For each: what did users see, what did monitoring show, how long to detect, what changed. Write it up as a postmortem in `docs/`.

You will hit alert fatigue immediately. Your first alert fires constantly and you start ignoring it. Tuning it is the lesson.

Done when you have three postmortems and one before/after performance number.

For the interview: "tell me about a time something broke" is a standard question, and you'll have three real answers with data. Almost nobody at your level does this.

## Phase 4 — The things that make it yours (M14–M16)

Everything here is optional. Ship Phase 3 first. A feature-rich laptop-only app is worth less than a plain one that's deployed and monitored.

### M14 — Recommendations

Goal: trips become advice other people can use.

Build

* Mark a place as recommended, with a note and a rating.
* A shareable list per trip or per city: "where I ate in Lisbon."
* Places as first-class records, separate from photos — several photos can map to one restaurant, and one restaurant can appear across several trips.

You will hit the entity resolution problem: three photos taken outside the same restaurant on different nights should collapse to one place, but your GPS readings differ by 30 metres each time. Clustering by proximity, then letting the user confirm, is the practical answer.

### M15 — Highlights

Goal: an auto-generated summary worth sending.

Build

* Pick 8–12 photos representing a trip. The selection logic is the interesting part: spread across distance rather than time (you shoot more at rest stops), favour points of interest and elevation change, drop near-duplicates.
* Perceptual hashing to collapse burst shots of the same view.
* An auto-written summary from the data: distance, duration, countries, the longest day, the highest point.

For the interview: "choose the 10 photos that best represent a trip" has no correct answer, so how you defined representative — and what failed — is a real design conversation.

### M16 — Planning

Goal: use past trips to plan future ones.

Be honest with yourself here: this is the vaguest part of the idea and the easiest to overbuild. Pick the smallest useful version.

The smallest useful version

* Search your own history by place: "show me everything from Portugal."
* Pull up the recommendations you or a friend saved for a city you're heading to.
* A rough itinerary from a past trip: what you did on which day, reusable as a template.

Only if the above is finished and you still care

* Browse public trips by destination.
* Seasonal signal from the atlas data and your own timestamps.

You will hit the cold-start problem — planning features need data you don't have yet. This is why it's last, and why the version that works on your own history is the one to build.

## The README (write it continuously, not at the end)

What gets read before your interview:

* One paragraph on what it does.
* An architecture diagram. ASCII is fine.
* Setup instructions someone can follow.
* Decisions. For each significant choice: what you picked, what you rejected, what the tradeoff was. Highest-value section in the repo.
* Known gaps. What's incomplete and what you'd do next. Listing your own gaps reads as judgment; claiming completeness reads as not having looked.

Write the decision entry when you make the decision. You will not remember in March.

## Checkpoints

* After M4 you have something that does a thing nothing else does.
* After M9 you have a demo that lands with a non-technical person.
* After M13 you have the project that gets you hired. This is the one that matters. If the semester eats you alive, be here.
* Phase 4 is the part that's yours.

Rough total: a full semester at 8–10 hours a week to end of M13. Phase 4 is extra, and it's fine to leave it as "next steps" in the README.

## Rules for yourself

1. Commit small and often, with messages explaining why.
2. When stuck: read the error, read the docs, then ask — about concepts, not solutions.
3. Write the decision down when you make it.
4. Features are seductive and infrastructure is what gets you hired. When tempted to add one more feature before deploying, don't.
5. No Kubernetes. A single-node cluster running one toy app is a red flag to anyone who has run a real one.
6. Billing alarms before resources. Every time.
7. If you can't explain a line of your code, delete it and write it again.
