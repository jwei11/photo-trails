"""The background worker: runs queued jobs. Run: python -m app.worker [--threads N]

Each thread loops: claim the next job, run its handler, record success, or record the
failure (retried with backoff, until it gives up). Ctrl-C or SIGTERM finishes the jobs
in progress, then exits; a hard kill leaves them to be reclaimed when their lease ends.

With WORKER_HEARTBEAT_FILE set (as in Docker), each thread touches that file whenever it
reaches the queue, so a healthcheck can tell a working worker from a hung or cut-off one.
"""
import argparse
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text

from app import cleanup, guests, imports, jobs, storage
from app.db import engine

log = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 0.5  # how long an idle thread waits before checking the queue again
SWEEP_EVERY = timedelta(minutes=15)
KEEP_FINISHED_JOBS = timedelta(days=7)


# ---- Handlers: one per job kind. Each must be safe to run twice. -----------------------

def process_photo(payload: dict) -> None:
    item_id = payload.get("item_id")
    try:
        storage.process_upload(payload["photo_id"])
        outcome = ("done", None, None, payload["photo_id"])
    except storage.DuplicatePhoto as dup:
        outcome = ("duplicate", None, None, dup.original_id)
    except storage.UploadNotFound:
        # The row is gone. Usually an expired upload; but a run that found a duplicate deletes
        # its row before recording "duplicate", so if it crashed in between, this rerun lands
        # here. The original's bytes were verified then; match it now by the declared hash.
        original = _original_for(payload)
        if original:
            outcome = ("duplicate", None, None, original)
        else:
            outcome = ("failed", "upload", "The upload expired. Upload the file again.", None)
    except storage.UploadNotReceived:
        outcome = ("failed", "upload", "Storage didn't receive the complete file.", payload["photo_id"])
    # Anything else (storage or database unreachable...) propagates: the job is retried.
    if item_id:
        imports.record_outcome(item_id, *outcome)


def _original_for(payload: dict) -> str | None:
    """The importing user's photo with the declared hash, if any."""
    if not payload.get("sha256") or not payload.get("item_id"):
        return None
    with engine.connect() as conn:
        user_id = conn.execute(
            text(
                "SELECT i.user_id FROM imports i JOIN import_items it ON it.import_id = i.id "
                "WHERE it.id = :item"
            ),
            {"item": payload["item_id"]},
        ).scalar_one_or_none()
    if user_id is None:
        return None
    return storage.find_by_hashes(str(user_id), [payload["sha256"]]).get(payload["sha256"])


def gave_up_on_photo(payload: dict, error: str) -> None:
    storage.mark_failed(payload["photo_id"], error)
    if payload.get("item_id"):
        imports.record_outcome(payload["item_id"], "failed", "processing", error, payload["photo_id"])


def regroup_trips(payload: dict) -> None:
    with engine.begin() as conn:
        storage.lock_writes(conn, payload["user_id"])
        storage.regroup_trips(conn, payload["user_id"])


def sweep(payload: dict) -> None:
    # Expired guests first: the files they uploaded become orphans, which the orphan sweep
    # removes once they're older than its cutoff.
    log.info("expiry %s", guests.delete_expired())
    cleanup.sweep()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM jobs WHERE status IN ('done', 'failed') AND finished_at < now() - :keep"),
            {"keep": KEEP_FINISHED_JOBS},
        )
        # The sweep schedules its own next run: a queue needs no separate scheduler.
        jobs.enqueue(conn, "sweep", dedupe_key="sweep", delay=SWEEP_EVERY)


HANDLERS: dict[str, Callable[[dict], None]] = {
    "process_photo": process_photo,
    "regroup_trips": regroup_trips,
    "sweep": sweep,
}
# Called once when a job fails for the last time, so the failure is visible where it matters.
ON_GIVE_UP: dict[str, Callable[[dict, str], None]] = {"process_photo": gave_up_on_photo}


# ---- The loop -------------------------------------------------------------------------

def run_one(worker_id: str) -> bool:
    """Claim and run one job. Returns False if there was nothing to run."""
    job = jobs.claim(worker_id, list(HANDLERS))
    if job is None:
        return False
    started = time.perf_counter()
    try:
        HANDLERS[job.kind](job.payload)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        gave_up = jobs.fail(job, worker_id, error)
        log.warning("job %s %s attempt %d/%d failed%s: %s", job.id, job.kind, job.attempts,
                    job.max_attempts, ", giving up" if gave_up else ", will retry", error)
        if gave_up and job.kind in ON_GIVE_UP:
            ON_GIVE_UP[job.kind](job.payload, error)
        return True
    jobs.succeed(job, worker_id)
    log.info("job %s %s done in %d ms", job.id, job.kind, (time.perf_counter() - started) * 1000)
    return True


def drain(worker_id: str = "drain") -> int:
    """Run every job that's runnable now, in this thread, then return how many ran. For tests
    and scripts: deterministic, no threads. Jobs scheduled for later are left alone."""
    ran = 0
    while run_one(worker_id):
        ran += 1
    return ran


def run(threads: int, stop: threading.Event, heartbeat: Path | None = None) -> None:
    with engine.begin() as conn:
        jobs.enqueue(conn, "sweep", dedupe_key="sweep")  # start the sweep cycle if it isn't
    worker_name = f"{socket.gethostname()}:{os.getpid()}"

    def loop(n: int) -> None:
        worker_id = f"{worker_name}:{n}"
        while not stop.is_set():
            try:
                ran = run_one(worker_id)
            except Exception:  # e.g. the database is down: don't die, wait and try again
                log.exception("worker %s couldn't reach the queue", worker_id)
                ran = False
            else:
                if heartbeat:
                    heartbeat.touch()
            if not ran:
                stop.wait(IDLE_POLL_SECONDS)

    # Threads, not processes: image decoding happens in C with Python's lock released, so
    # threads process photos in parallel (measured in M3: 4 threads ran 3.4x faster).
    pool = [threading.Thread(target=loop, args=(n,), name=f"worker-{n}") for n in range(threads)]
    for t in pool:
        t.start()
    log.info("worker %s running %d threads", worker_name, threads)
    for t in pool:
        t.join()
    log.info("worker %s stopped", worker_name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())  # finish current jobs, then exit
    heartbeat = os.environ.get("WORKER_HEARTBEAT_FILE")
    run(args.threads, stop, Path(heartbeat) if heartbeat else None)
