"""app releases

Revision ID: a91f5c027d64
Revises: f4c1d83a52b7
Created: 2026-08-31 10:00:00.000000+00:00

One new table. Nothing else is touched, and no row is inserted: an empty
``app_releases`` means ``GET /app/release`` answers "no update" to everybody,
which is exactly the state this should ship in. The first release is recorded
from the console once the build is actually in the stores.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a91f5c027d64"
down_revision: str | None = "f4c1d83a52b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_releases",
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("minimum_version", sa.String(length=32), nullable=False),
        sa.Column("store_url", sa.String(length=300), nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_releases")),
        sa.UniqueConstraint(
            "platform", "version", name="uq_app_releases_platform_version"
        ),
    )
    # The lookup every launch does: newest published row for one platform.
    op.create_index(
        "ix_app_releases_platform_published",
        "app_releases",
        ["platform", "published"],
    )


def downgrade() -> None:
    op.drop_index("ix_app_releases_platform_published", table_name="app_releases")
    op.drop_table("app_releases")
