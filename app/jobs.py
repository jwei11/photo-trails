"""A job queue in a Postgres table.

Job status: queued -> running -> done | failed, and running -> queued again on a retry.

Enqueue inside the transaction that makes the work necessary: the job then exists exactly
when that change does. A worker claims a job by locking its row (FOR UPDATE SKIP LOCKED)
and taking a lease: until locked_until, the job is that worker's. If the worker crashes,
the lease runs out and the job is claimable again, so work is never stuck, but it may run
twice. Handlers must therefore be idempotent: running one again changes nothing.
"""
import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Connection, text

from app.db import engine

LEASE = timedelta(minutes=5)  # far longer than any job takes; only matters after a crash
RETRY_BASE = timedelta(seconds=10)  # attempt n waits 10s * 2^(n-1): 10s, 20s, 40s...


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int  # including this one
    max_attempts: int

    @property
    def last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


def enqueue(
    conn: Connection, kind: str, payload: dict | None = None, *,
    dedupe_key: str | None = None, delay: timedelta = timedelta(0), priority: int = 0,
) -> bool:
    """Queue a job, in the caller's transaction. With a dedupe_key, nothing is added if the
    same job is already waiting. Lower priorities run first (see app.limits.job_priority).
    Returns whether a job was added."""
    return bool(
        conn.execute(
            text(
                """
                INSERT INTO jobs (kind, payload, dedupe_key, run_after, priority)
                VALUES (:kind, CAST(:payload AS jsonb), :key, now() + :delay, :priority)
                ON CONFLICT (kind, dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL
                DO NOTHING
                """
            ),
            {"kind": kind, "payload": json.dumps(payload or {}), "key": dedupe_key, "delay": delay,
             "priority": priority},
        ).rowcount
    )


def claim(worker_id: str, kinds: list[str] | None = None) -> Job | None:
    """Take the next runnable job: a queued one whose time has come, or a running one whose
    worker's lease has expired (that worker died). None if there's nothing to do."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                       locked_until = now() + :lease, locked_by = :worker
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE ((status = 'queued' AND run_after <= now())
                           OR (status = 'running' AND locked_until < now()))
                      AND (CAST(:kinds AS text[]) IS NULL OR kind = ANY(CAST(:kinds AS text[])))
                    -- Higher-priority work first, however long lower-priority work has
                    -- waited: a burst of demo imports never delays an invited user's.
                    ORDER BY priority, run_after, id
                    -- Lock the row we take; rows other workers have locked are skipped,
                    -- not waited for, so workers never block each other or double-claim.
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, kind, payload, attempts, max_attempts
                """
            ),
            {"lease": LEASE, "worker": worker_id, "kinds": kinds},
        ).first()
    return Job(row.id, row.kind, row.payload, row.attempts, row.max_attempts) if row else None


def succeed(job: Job, worker_id: str) -> None:
    with engine.begin() as conn:
        _finish(conn, job, worker_id, "status = 'done', finished_at = now(), last_error = NULL")


def fail(job: Job, worker_id: str, error: str) -> bool:
    """Record a failed attempt: retry later with backoff, or give up after the last attempt.
    Returns True only if this worker recorded the job as given up: one that lost the job to
    another worker mustn't act on the failure (the other may yet succeed)."""
    with engine.begin() as conn:
        if job.last_attempt:
            return _finish(conn, job, worker_id, "status = 'failed', finished_at = now(), last_error = :error",
                           error=error[:2000])
        _finish(
            conn, job, worker_id,
            "status = 'queued', run_after = now() + :backoff, locked_until = NULL, locked_by = NULL,"
            " last_error = :error",
            error=error[:2000], backoff=RETRY_BASE * 2 ** (job.attempts - 1),
        )
        return False


def _finish(conn: Connection, job: Job, worker_id: str, assignments: str, **params) -> bool:
    # Only the worker holding the job may record its outcome: if this worker was so slow
    # that its lease expired and another worker took the job over, its result is dropped.
    # Returns whether the outcome was recorded.
    return bool(conn.execute(
        text(f"UPDATE jobs SET {assignments} WHERE id = :id AND locked_by = :worker AND status = 'running'"),
        {"id": job.id, "worker": worker_id, **params},
    ).rowcount)


def counts() -> dict[str, int]:
    """Jobs by status: queue depth for monitoring (M13)."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT status, count(*) FROM jobs GROUP BY status")).all()
    return {status: n for status, n in rows}
