"""forbid overlapping clock offsets

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # btree_gist lets a GiST index combine equality (=) on text columns with range overlap (&&).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
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


def downgrade() -> None:
    op.execute(
        "ALTER TABLE camera_clock_offsets DROP CONSTRAINT camera_clock_offsets_no_overlap"
    )
