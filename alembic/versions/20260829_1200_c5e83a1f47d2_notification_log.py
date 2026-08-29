"""notification log

Revision ID: c5e83a1f47d2
Revises: a3f7c21b8e40
Created: 2026-08-29 12:00:00.000000+00:00

One new table and no change to any existing one, so a rolling deploy is safe:
the release still running does not know it exists.

The unique constraint is the point of the table, not an afterthought. It is what
makes a reminder go out once when two workers overlap during a deploy — the
sweep claims a row before it sends, and a collision means somebody else already
did. Adding it later, after duplicates existed, would mean cleaning them out by
hand first.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5e83a1f47d2"
down_revision: str | None = "a3f7c21b8e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_log",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("dedupe_key", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=300), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_notification_log_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_log")),
        sa.UniqueConstraint(
            "user_id", "dedupe_key", name="notification_log_user_key"
        ),
    )
    op.create_index(
        op.f("ix_notification_log_user_id"), "notification_log", ["user_id"]
    )
    op.create_index(
        "ix_notification_log_user_created",
        "notification_log",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_log_user_created", table_name="notification_log")
    op.drop_index(op.f("ix_notification_log_user_id"), table_name="notification_log")
    op.drop_table("notification_log")
