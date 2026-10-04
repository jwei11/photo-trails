"""The job queue's guarantees: claimed once, recovered after a crash, safe to run twice,
coalesced where asked, retried then given up on."""
import threading
from datetime import timedelta

from sqlalchemy import text

from app import jobs, storage, worker
from app.db import engine
from tests.test_api import jpeg
from tests.test_imports import complete, describe, put, start_import


def photo(hour, where=(38.72, -9.14)):
    return jpeg(f"2025:07:01 {hour:02d}:00:00", "+01:00", *where)


def job_rows(kind=None):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT kind, status, attempts, locked_by FROM jobs WHERE CAST(:kind AS text) IS NULL OR kind = :kind ORDER BY id"),
            {"kind": kind},
        ).all()


def photo_status(photo_id):
    with engine.connect() as conn:
        return conn.execute(text("SELECT status FROM photos WHERE id = :id"), {"id": photo_id}).scalar()


def uploaded_import(client, *datas):
    imp = start_import(client, *(describe(d, name=f"{n}.jpg") for n, d in enumerate(datas)))
    for item, data in zip(imp["items"], datas):
        assert put(item, data).status_code == 200
    return imp


def test_complete_returns_at_once_and_the_worker_does_the_processing(client):
    data = photo(10)
    imp = uploaded_import(client, data)
    item = imp["items"][0]

    queued = complete(client, imp, item, process=False)

    assert queued["status"] == "processing"
    assert photo_status(item["photo_id"]) == "uploaded"
    assert [(k, s) for k, s, *_ in job_rows()] == [("process_photo", "queued")]

    worker.drain()

    assert photo_status(item["photo_id"]) == "ready"
    assert client.get(f"/api/imports/{imp['id']}").json()["items"][0]["status"] == "done"


def test_an_import_regroups_trips_once_not_after_every_photo(client):
    imp = uploaded_import(client, *(photo(h) for h in range(9, 14)))
    for item in imp["items"]:
        complete(client, imp, item, process=False)

    worker.drain()

    # Each photo asked for a regroup; while one waited, the rest were absorbed into it.
    assert [s for _, s, *_ in job_rows("regroup_trips")] == ["done"]
    trips = client.get("/api/trips").json()
    assert len(trips) == 1 and trips[0]["photo_count"] == 5


def test_a_job_whose_worker_crashed_is_taken_over_when_its_lease_runs_out(client):
    imp = uploaded_import(client, photo(10))
    item = imp["items"][0]
    complete(client, imp, item, process=False)

    crashed = jobs.claim("worker-that-dies")  # claims the job, then "dies": never finishes it
    assert jobs.claim("worker-b") is None  # while the lease holds, nobody else can take it
    with engine.begin() as conn:  # time passes: the 5-minute lease runs out
        conn.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))

    assert worker.drain("worker-b") >= 1
    assert photo_status(item["photo_id"]) == "ready"
    (job,) = job_rows("process_photo")
    assert (job.status, job.attempts, job.locked_by) == ("done", 2, "worker-b")

    # The crashed worker comes back and reports success: ignored, it no longer holds the job.
    jobs.succeed(crashed, "worker-that-dies")
    assert job_rows("process_photo")[0].locked_by == "worker-b"


def test_running_a_photo_job_twice_changes_nothing(client):
    imp = uploaded_import(client, photo(10))
    item = imp["items"][0]
    complete(client, imp, item)
    before = client.get(f"/api/photos/{item['photo_id']}/metadata").json()

    # A rerun, as after a crash between finishing the work and recording it.
    worker.process_photo({"photo_id": item["photo_id"], "item_id": item["id"]})

    after = client.get(f"/api/photos/{item['photo_id']}/metadata").json()
    assert after == before
    assert len(client.get("/api/photos").json()) == 1


def test_workers_skip_jobs_another_worker_has_locked(client):
    with engine.begin() as conn:
        jobs.enqueue(conn, "sweep")
        jobs.enqueue(conn, "regroup_trips")
    first_id = job_rows()[0]
    with engine.connect() as holder:
        # Another worker is mid-claim on the first job: its row is locked.
        holder.execute(text("SELECT id FROM jobs ORDER BY id LIMIT 1 FOR UPDATE"))
        claimed = jobs.claim("worker-b")  # doesn't wait for the lock: takes the next job
        assert claimed is not None and claimed.kind == "regroup_trips"
        holder.rollback()
    assert first_id.kind == "sweep"


def test_a_job_with_a_waiting_twin_is_not_added_again(client):
    with engine.begin() as conn:
        assert jobs.enqueue(conn, "regroup_trips", dedupe_key="all")
        assert not jobs.enqueue(conn, "regroup_trips", dedupe_key="all")
    running = jobs.claim("w")
    with engine.begin() as conn:  # new work arriving while it runs does get its own job
        assert jobs.enqueue(conn, "regroup_trips", dedupe_key="all")
    assert running is not None
    assert [s for _, s, *_ in job_rows("regroup_trips")] == ["running", "queued"]


def test_a_failing_job_is_retried_later_then_given_up_and_shown(client, monkeypatch):
    imp = uploaded_import(client, photo(10))
    item = imp["items"][0]
    complete(client, imp, item, process=False)
    monkeypatch.setattr(storage, "process_upload", lambda *a, **k: (_ for _ in ()).throw(OSError("S3 timeout")))

    worker.drain()  # first attempt fails; the retry is scheduled 10s later, so drain stops
    (job,) = job_rows("process_photo")
    assert (job.status, job.attempts) == ("queued", 1)
    assert photo_status(item["photo_id"]) != "failed"  # one bad attempt isn't a failure yet

    monkeypatch.setattr(jobs, "RETRY_BASE", timedelta(0))
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET run_after = now()"))
    worker.drain()

    assert job_rows("process_photo")[0].status == "failed"
    assert photo_status(item["photo_id"]) == "failed"
    shown = client.get(f"/api/imports/{imp['id']}").json()["items"][0]
    assert (shown["status"], shown["failed_stage"]) == ("failed", "processing")
    assert "S3 timeout" in shown["error"]


def test_the_sweep_reschedules_itself_and_forgets_old_jobs(client):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO jobs (kind, status, finished_at) VALUES ('regroup_trips', 'done', now() - interval '8 days')"
        ))
        jobs.enqueue(conn, "sweep", dedupe_key="sweep")

    worker.drain()

    rows = job_rows()
    assert ("regroup_trips", "done") not in [(k, s) for k, s, *_ in rows]  # older than 7 days: gone
    assert [(k, s) for k, s, *_ in rows] == [("sweep", "done"), ("sweep", "queued")]  # next run booked


def test_a_duplicate_rerun_after_a_crash_still_says_duplicate(client):
    data = photo(10)
    imp = uploaded_import(client, data)
    complete(client, imp, imp["items"][0])  # the original
    again = start_import(client, describe(data, name="again.jpg"), describe(photo(11), name="other.jpg"))
    dup_item = next(i for i in again["items"] if i["filename"] == "again.jpg")
    # The browser skips known files; another client might not, so force an upload of it.
    item = again["items"][1]
    assert put(item, photo(11)).status_code == 200
    complete(client, again, item, process=False)
    job_payload = {"photo_id": item["photo_id"], "item_id": item["id"]}

    # A run that found a duplicate deleted the photo row, then crashed before recording it:
    with engine.begin() as conn:
        original_id = conn.execute(text("SELECT id::text FROM photos WHERE status = 'ready'")).scalar()
        sha = conn.execute(text("SELECT content_sha256 FROM photos WHERE status = 'ready'")).scalar()
        conn.execute(text("DELETE FROM photos WHERE id = :id"), {"id": item["photo_id"]})
    worker.process_photo({**job_payload, "sha256": sha})

    shown = next(i for i in client.get(f"/api/imports/{again['id']}").json()["items"] if i["id"] == item["id"])
    assert (shown["status"], shown["photo_id"]) == ("duplicate", original_id)
    assert dup_item["status"] == "duplicate"  # (the exact copy was caught at registration)


def test_a_worker_that_lost_its_job_does_not_mark_the_photo_failed(client, monkeypatch):
    imp = uploaded_import(client, photo(10))
    item = imp["items"][0]
    complete(client, imp, item, process=False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET max_attempts = 1"))

    slow = jobs.claim("slow-worker")
    with engine.begin() as conn:  # its lease runs out; another worker takes the job over
        conn.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second', max_attempts = 2"))
    taken_over = jobs.claim("worker-b")
    assert taken_over is not None

    # The slow worker now fails what it believes is the last attempt.
    assert jobs.fail(slow, "slow-worker", "timeout") is False  # not its job any more: no give-up

    assert photo_status(item["photo_id"]) != "failed"
    assert job_rows("process_photo")[0].locked_by == "worker-b"


def run_briefly(heartbeat):
    stop = threading.Event()
    threading.Timer(1.0, stop.set).start()
    worker.run(1, stop, heartbeat=heartbeat)


def test_the_worker_touches_its_heartbeat_file_while_it_can_reach_the_queue(client, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    run_briefly(heartbeat)
    assert heartbeat.exists()


def test_the_heartbeat_stops_when_the_queue_is_unreachable(client, tmp_path, monkeypatch):
    def unreachable(worker_id):
        raise OSError("database is down")

    monkeypatch.setattr(worker, "run_one", unreachable)
    heartbeat = tmp_path / "heartbeat"
    run_briefly(heartbeat)
    assert not heartbeat.exists()
