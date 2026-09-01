"""material notified_at

Revision ID: b6d2e19f84c3
Revises: a91f5c027d64
Created: 2026-09-01 09:00:00.000000+00:00

One nullable column. Nullable with no default and no backfill, so the release
still running does not know it exists and a rolling deploy is safe.

Deliberately not backfilled. NULL means "the student has not been told", which
is true of every row already in the table.

That does not announce the backlog on deploy, because the sweep also requires a
material to have finished inside `FINISHED_WINDOW` -- see
`app/services/notifications.py`. Without that window this column would need a
backfill to `now()`, and the two would have to agree for ever; with it, the
freshness rule lives in one place and this migration is one nullable column.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6d2e19f84c3"
down_revision: str | None = "a91f5c027d64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "materials",
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("materials", "notified_at")
