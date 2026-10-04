"""add a job queue and processing states

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The queue. A worker claims the oldest runnable job with FOR UPDATE SKIP LOCKED and
    # holds a lease (locked_until) while running it; if the worker dies, the lease runs
    # out and another worker takes the job over. Finished jobs are kept for a while as a
    # record, then deleted by the sweep.
    op.execute(
        """
        CREATE TABLE jobs (
            id           bigserial PRIMARY KEY,
            kind         text NOT NULL,
            payload      jsonb NOT NULL DEFAULT '{}',
            -- Jobs with the same kind and key are the same piece of work: at most one of
            -- them can be waiting at a time (see the unique index below).
            dedupe_key   text,
            status       text NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'running', 'done', 'failed')),
            attempts     integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 3,
            run_after    timestamptz NOT NULL DEFAULT now(),
            locked_until timestamptz,
            locked_by    text,
            last_error   text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            finished_at  timestamptz
        )
        """
    )
    # Claiming only ever looks at waiting or running jobs, ordered by run_after.
    op.execute(
        "CREATE INDEX jobs_claimable_idx ON jobs (run_after) WHERE status IN ('queued', 'running')"
    )
    # Coalescing: a second "regroup trips" or "process photo X" request while one is still
    # waiting adds nothing (INSERT ... ON CONFLICT DO NOTHING). Running jobs aren't
    # covered, so work arriving during a run gets a fresh job and isn't missed.
    op.execute(
        """
        CREATE UNIQUE INDEX jobs_one_waiting_idx ON jobs (kind, dedupe_key)
        WHERE status = 'queued' AND dedupe_key IS NOT NULL
        """
    )

    # Photo lifecycle: pending (row and upload URL exist) -> uploaded (file is in storage,
    # job queued) -> processing (a worker has it) -> ready | failed.
    op.execute("ALTER TABLE photos DROP CONSTRAINT photos_status_check")
    op.execute(
        """
        ALTER TABLE photos ADD CONSTRAINT photos_status_check
            CHECK (status IN ('pending', 'uploaded', 'processing', 'ready', 'failed'))
        """
    )
    # Import items gain 'processing': uploaded, waiting for or in the worker.
    op.execute("ALTER TABLE import_items DROP CONSTRAINT import_items_status_check")
    op.execute(
        """
        ALTER TABLE import_items ADD CONSTRAINT import_items_status_check
            CHECK (status IN ('uploading', 'processing', 'done', 'duplicate', 'failed'))
        """
    )


def downgrade() -> None:
    # Photos still mid-processing go back to pending: their upload is redone or swept.
    op.execute("UPDATE photos SET status = 'pending' WHERE status IN ('uploaded', 'processing', 'failed')")
    op.execute("UPDATE import_items SET status = 'uploading' WHERE status = 'processing'")
    op.execute("ALTER TABLE import_items DROP CONSTRAINT import_items_status_check")
    op.execute(
        """
        ALTER TABLE import_items ADD CONSTRAINT import_items_status_check
            CHECK (status IN ('uploading', 'done', 'duplicate', 'failed'))
        """
    )
    op.execute("ALTER TABLE photos DROP CONSTRAINT photos_status_check")
    op.execute("ALTER TABLE photos ADD CONSTRAINT photos_status_check CHECK (status IN ('pending', 'ready'))")
    op.execute("DROP TABLE jobs")
