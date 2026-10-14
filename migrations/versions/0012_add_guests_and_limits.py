"""add account kinds, guest accounts, demo copies, job priority, and rate limits

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-14
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # How an account came to exist, which decides its limits:
    #   invited: created with `python -m app.users create` (no limits)
    #   signup:  signed up with Google while sign-ups were open (quota)
    #   guest:   "Try the demo", a copy of the demo trip, deleted when it expires (quota)
    #   demo:    holds the demo trip that guests get a copy of; nobody signs in as it
    # Only guests expire.
    op.execute(
        """
        ALTER TABLE users
            ADD COLUMN kind text NOT NULL DEFAULT 'invited'
                CHECK (kind IN ('invited', 'signup', 'guest', 'demo')),
            ADD COLUMN expires_at timestamptz,
            ADD CONSTRAINT users_only_guests_expire CHECK ((kind = 'guest') = (expires_at IS NOT NULL))
        """
    )
    op.execute("CREATE INDEX users_guest_expiry_idx ON users (expires_at) WHERE kind = 'guest'")
    op.execute("CREATE UNIQUE INDEX users_one_demo_idx ON users (kind) WHERE kind = 'demo'")

    # A guest's copy of a demo photo. Its files belong to the demo account: the copy points
    # at the same storage keys, so starting a demo copies rows, not images.
    op.execute("ALTER TABLE photos ADD COLUMN from_demo boolean NOT NULL DEFAULT false")
    # A file still has exactly one owning row; only the copies share it.
    op.execute("ALTER TABLE photos DROP CONSTRAINT photos_storage_key_key")
    op.execute("CREATE UNIQUE INDEX photos_storage_key_key ON photos (storage_key) WHERE NOT from_demo")

    # Lower numbers run first: invited users' work before guests' and sign-ups'.
    op.execute("ALTER TABLE jobs ADD COLUMN priority smallint NOT NULL DEFAULT 0")
    op.execute("DROP INDEX jobs_claimable_idx")
    op.execute(
        "CREATE INDEX jobs_claimable_idx ON jobs (priority, run_after) WHERE status IN ('queued', 'running')"
    )

    # Who did what recently, for rate limits. The key is a keyed hash of an IP address,
    # never the address itself, and rows are deleted after a day.
    op.execute(
        """
        CREATE TABLE rate_limit_events (
            action     text NOT NULL,
            key_hash   bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX rate_limit_events_key_idx ON rate_limit_events (action, key_hash, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE rate_limit_events")
    op.execute("DROP INDEX jobs_claimable_idx")
    op.execute("ALTER TABLE jobs DROP COLUMN priority")
    op.execute("CREATE INDEX jobs_claimable_idx ON jobs (run_after) WHERE status IN ('queued', 'running')")
    # Guests, sign-ups, and the demo have no place in the old schema.
    op.execute("DELETE FROM users WHERE kind <> 'invited'")
    op.execute("DROP INDEX photos_storage_key_key")
    op.execute("ALTER TABLE photos ADD CONSTRAINT photos_storage_key_key UNIQUE (storage_key)")
    op.execute("ALTER TABLE photos DROP COLUMN from_demo")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_only_guests_expire")
    op.execute("ALTER TABLE users DROP COLUMN expires_at, DROP COLUMN kind")
