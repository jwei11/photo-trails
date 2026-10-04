"""add photo upload status

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows were uploaded through the API, so they're complete.
    op.execute(
        """
        ALTER TABLE photos ADD COLUMN status text NOT NULL DEFAULT 'ready'
            CHECK (status IN ('pending', 'ready'))
        """
    )
    # Only pending rows are ever searched by status (the orphan sweep), so index just those.
    op.execute("CREATE INDEX photos_pending_idx ON photos (created_at) WHERE status = 'pending'")


def downgrade() -> None:
    op.execute("DROP INDEX photos_pending_idx")
    op.execute("ALTER TABLE photos DROP COLUMN status")
