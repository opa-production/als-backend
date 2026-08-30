"""feature requests

Revision ID: d7a4b62e19c3
Revises: c5e83a1f47d2
Created: 2026-08-30 14:00:00.000000+00:00

One new table, nothing existing touched, so a rolling deploy is safe: the
release still running does not know it exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7a4b62e19c3"
down_revision: str | None = "c5e83a1f47d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_requests",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("app_version", sa.String(length=32), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_feature_requests_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_requests")),
    )
    op.create_index(
        op.f("ix_feature_requests_user_id"), "feature_requests", ["user_id"]
    )
    op.create_index(
        "ix_feature_requests_user_created",
        "feature_requests",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_feature_requests_created", "feature_requests", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_feature_requests_created", table_name="feature_requests")
    op.drop_index("ix_feature_requests_user_created", table_name="feature_requests")
    op.drop_index(op.f("ix_feature_requests_user_id"), table_name="feature_requests")
    op.drop_table("feature_requests")
