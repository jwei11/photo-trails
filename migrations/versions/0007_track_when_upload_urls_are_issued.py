"""track when upload urls are issued

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An import creates every pending photo up front, so created_at says nothing about
    # whether an upload is still active. The last URL issue time does: URLs expire.
    op.execute("ALTER TABLE photos ADD COLUMN upload_url_issued_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE photos DROP COLUMN upload_url_issued_at")
