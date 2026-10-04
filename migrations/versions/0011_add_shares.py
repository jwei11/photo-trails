"""add share links for trips

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-03
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A share is a link to a read-only view of one trip. The token is the link: random, so
    # it can't be guessed or counted up to like an id. Revoking keeps the row (so the owner
    # sees what they shared) but the link stops working. The foreign key covers the owner
    # too, so a share always points at its owner's own trip, and deleting the trip (say,
    # dissolving it) deletes its shares.
    op.execute(
        """
        CREATE TABLE shares (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            trip_id     uuid NOT NULL,
            token       text NOT NULL UNIQUE CHECK (length(token) >= 32),
            precision   text NOT NULL DEFAULT 'city' CHECK (precision IN ('city', 'neighbourhood')),
            exact_dates boolean NOT NULL DEFAULT false,
            created_at  timestamptz NOT NULL DEFAULT now(),
            revoked_at  timestamptz,
            FOREIGN KEY (trip_id, user_id) REFERENCES trips (id, user_id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX shares_trip_id_idx ON shares (trip_id)")


def downgrade() -> None:
    op.execute("DROP TABLE shares")
