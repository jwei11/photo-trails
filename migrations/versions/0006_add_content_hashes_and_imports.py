"""add content hashes and imports

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Verified SHA-256 of the original's bytes, set when processing completes. Unique, so
    # two identical files completing at the same moment can't both be stored.
    op.execute(
        """
        ALTER TABLE photos ADD COLUMN content_sha256 text
            CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX photos_content_sha256_key ON photos (content_sha256) "
        "WHERE content_sha256 IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE imports (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE import_items (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            import_id       uuid NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
            position        integer NOT NULL,
            filename        text,
            content_type    text NOT NULL,
            size_bytes      bigint NOT NULL CHECK (size_bytes > 0),
            declared_sha256 text NOT NULL CHECK (declared_sha256 ~ '^[0-9a-f]{64}$'),
            status          text NOT NULL CHECK (status IN ('uploading', 'done', 'duplicate', 'failed')),
            failed_stage    text CHECK (failed_stage IN ('upload', 'processing')),
            error           text,
            -- The photo this file became, or for a duplicate, the photo it duplicates.
            photo_id        uuid REFERENCES photos (id) ON DELETE SET NULL,
            updated_at      timestamptz NOT NULL DEFAULT now(),
            UNIQUE (import_id, position),
            CHECK ((status = 'failed') = (failed_stage IS NOT NULL))
        )
        """
    )
    op.execute("CREATE INDEX import_items_import_id_idx ON import_items (import_id)")


def downgrade() -> None:
    op.execute("DROP TABLE import_items")
    op.execute("DROP TABLE imports")
    op.execute("DROP INDEX photos_content_sha256_key")
    op.execute("ALTER TABLE photos DROP COLUMN content_sha256")
