"""create photos table

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute(
        """
        CREATE TABLE photos (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            original_filename text,
            content_type      text NOT NULL,
            size_bytes        bigint NOT NULL CHECK (size_bytes >= 0),
            storage_key       text NOT NULL UNIQUE,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX photos_created_at_idx ON photos (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE photos")
