"""add users and sessions, and an owner on everything

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-03

Existing rows need an owner. If there are any, set OWNER_EMAIL to the email you'll sign in
with, and the migration creates that user and gives them everything:
    OWNER_EMAIL=you@example.com .venv/bin/alembic upgrade head
"""
import os

from alembic import op
from sqlalchemy import text

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

OWNED = ("photos", "trips", "homes", "camera_clock_offsets", "imports")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Stored lowercased, so lookups and the unique index ignore case.
            email      text NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '_%@_%'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Only a hash of each token is stored: someone who reads the database (a backup, a
    # leaked dump) can't use it to sign in as anyone.
    op.execute(
        """
        CREATE TABLE sessions (
            token_hash   bytea PRIMARY KEY,
            user_id      uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            created_at   timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX sessions_user_id_idx ON sessions (user_id)")
    op.execute(
        """
        CREATE TABLE login_links (
            token_hash bytea PRIMARY KEY,
            user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            expires_at timestamptz NOT NULL,
            used_at    timestamptz
        )
        """
    )

    conn = op.get_bind()
    has_rows = any(
        conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one() for table in OWNED
    )
    owner = None
    if has_rows:
        email = os.environ.get("OWNER_EMAIL", "").strip().lower()
        if not email:
            raise RuntimeError(
                "This database has photos, trips, or settings that need an owner. Run the "
                "migration with OWNER_EMAIL set to the email you'll sign in with, e.g. "
                "OWNER_EMAIL=you@example.com alembic upgrade head"
            )
        owner = conn.execute(
            text("INSERT INTO users (email) VALUES (:email) RETURNING id"), {"email": email}
        ).scalar_one()

    for table in OWNED:
        op.execute(f"ALTER TABLE {table} ADD COLUMN user_id uuid REFERENCES users (id) ON DELETE CASCADE")
        if owner:
            conn.execute(text(f"UPDATE {table} SET user_id = :owner"), {"owner": owner})
        op.execute(f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL")
        op.execute(f"CREATE INDEX {table}_user_id_idx ON {table} (user_id)")

    # A photo can only be in a trip of the same user: the foreign key covers both columns,
    # so the database itself refuses a mix-up, whatever the code does. Deleting a trip
    # clears only the photo's trip_id, never its owner.
    op.execute("ALTER TABLE trips ADD CONSTRAINT trips_id_user_id_key UNIQUE (id, user_id)")
    op.execute("ALTER TABLE photos DROP CONSTRAINT photos_trip_id_fkey")
    op.execute(
        """
        ALTER TABLE photos ADD CONSTRAINT photos_trip_fkey FOREIGN KEY (trip_id, user_id)
            REFERENCES trips (id, user_id) ON DELETE SET NULL (trip_id)
        """
    )

    # Duplicates are per user. A shared index would also tell one user that another already
    # has a photo, which is itself a leak.
    op.execute("DROP INDEX photos_content_sha256_key")
    op.execute(
        "CREATE UNIQUE INDEX photos_user_content_sha256_key ON photos (user_id, content_sha256) "
        "WHERE content_sha256 IS NOT NULL"
    )

    # Two people can own the same camera model; their clock offsets are separate.
    op.execute("ALTER TABLE camera_clock_offsets DROP CONSTRAINT camera_clock_offsets_no_overlap")
    op.execute(
        """
        ALTER TABLE camera_clock_offsets
            ADD CONSTRAINT camera_clock_offsets_no_overlap EXCLUDE USING gist (
                user_id WITH =,
                camera_make WITH =,
                camera_model WITH =,
                coalesce(camera_serial, '') WITH =,
                tsrange(valid_from, valid_to) WITH &&
            )
        """
    )

    # Regroups are now per user. A waiting regroup of everything becomes one per owner.
    op.execute(
        """
        INSERT INTO jobs (kind, payload, dedupe_key)
        SELECT 'regroup_trips', jsonb_build_object('user_id', u.id), u.id::text FROM users u
        WHERE EXISTS (SELECT 1 FROM jobs WHERE kind = 'regroup_trips' AND status = 'queued')
        """
    )
    op.execute("DELETE FROM jobs WHERE kind = 'regroup_trips' AND status = 'queued' AND dedupe_key = 'all'")


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'regroup_trips' AND status = 'queued'")
    op.execute("INSERT INTO jobs (kind, dedupe_key) VALUES ('regroup_trips', 'all')")
    op.execute("ALTER TABLE camera_clock_offsets DROP CONSTRAINT camera_clock_offsets_no_overlap")
    op.execute(
        """
        ALTER TABLE camera_clock_offsets
            ADD CONSTRAINT camera_clock_offsets_no_overlap EXCLUDE USING gist (
                camera_make WITH =,
                camera_model WITH =,
                coalesce(camera_serial, '') WITH =,
                tsrange(valid_from, valid_to) WITH &&
            )
        """
    )
    op.execute("DROP INDEX photos_user_content_sha256_key")
    op.execute(
        "CREATE UNIQUE INDEX photos_content_sha256_key ON photos (content_sha256) "
        "WHERE content_sha256 IS NOT NULL"
    )
    op.execute("ALTER TABLE photos DROP CONSTRAINT photos_trip_fkey")
    op.execute(
        "ALTER TABLE photos ADD CONSTRAINT photos_trip_id_fkey FOREIGN KEY (trip_id) "
        "REFERENCES trips (id) ON DELETE SET NULL"
    )
    op.execute("ALTER TABLE trips DROP CONSTRAINT trips_id_user_id_key")
    for table in OWNED:
        op.execute(f"ALTER TABLE {table} DROP COLUMN user_id")
    op.execute("DROP TABLE login_links")
    op.execute("DROP TABLE sessions")
    op.execute("DROP TABLE users")
