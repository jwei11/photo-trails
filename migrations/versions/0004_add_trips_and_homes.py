"""add trips and homes

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trips (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name       text CHECK (length(name) BETWEEN 1 AND 200),
            is_locked  boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        ALTER TABLE photos
            ADD COLUMN trip_id     uuid REFERENCES trips (id) ON DELETE SET NULL,
            ADD COLUMN trip_pinned boolean NOT NULL DEFAULT false
        """
    )
    op.execute("CREATE INDEX photos_trip_id_idx ON photos (trip_id)")
    op.execute(
        """
        CREATE TABLE homes (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
            lat        double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
            lon        double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
            radius_km  double precision NOT NULL DEFAULT 25 CHECK (radius_km > 0 AND radius_km <= 500),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE homes")
    op.execute("ALTER TABLE photos DROP COLUMN trip_pinned, DROP COLUMN trip_id")
    op.execute("DROP TABLE trips")
