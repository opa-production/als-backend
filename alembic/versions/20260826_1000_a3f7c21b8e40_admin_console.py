"""admin console

Revision ID: a3f7c21b8e40
Revises: b1d9e2936d24
Created: 2026-08-26 10:00:00.000000+00:00

Three new tables and no change to any existing one, which is what makes this
safe for a rolling deploy: containers still running the previous release do not
know these tables exist and are unaffected by their existence.

Nothing is seeded here. The first administrator is created by
``scripts/create_admin.py``, deliberately — a default account with a known
password baked into a migration is a back door that ships to every environment
and is forgotten in most of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3f7c21b8e40"
down_revision: str | None = "b1d9e2936d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
    )
    op.create_index(
        op.f("ix_admin_users_email"), "admin_users", ["email"], unique=True
    )

    op.create_table(
        "admin_refresh_tokens",
        sa.Column("admin_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
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
            ["admin_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_refresh_tokens_admin_id_admin_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_refresh_tokens")),
        sa.UniqueConstraint(
            "token_hash", name=op.f("uq_admin_refresh_tokens_token_hash")
        ),
    )
    op.create_index(
        op.f("ix_admin_refresh_tokens_admin_id"),
        "admin_refresh_tokens",
        ["admin_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_refresh_tokens_admin_revoked",
        "admin_refresh_tokens",
        ["admin_id", "revoked_at"],
        unique=False,
    )

    op.create_table(
        "admin_audit_log",
        # SET NULL, not CASCADE. Removing an administrator must not erase the
        # record of what they did; ``admin_email`` is the copy that survives.
        sa.Column("admin_id", sa.Uuid(), nullable=True),
        sa.Column("admin_email", sa.String(length=320), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("ip", sa.String(length=64), nullable=True),
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
            ["admin_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_audit_log_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_audit_log")),
    )
    op.create_index(
        op.f("ix_admin_audit_log_admin_id"), "admin_audit_log", ["admin_id"], unique=False
    )
    op.create_index(
        op.f("ix_admin_audit_log_action"), "admin_audit_log", ["action"], unique=False
    )
    op.create_index(
        op.f("ix_admin_audit_log_target_id"),
        "admin_audit_log",
        ["target_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_log_created", "admin_audit_log", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_log_created", table_name="admin_audit_log")
    op.drop_index(op.f("ix_admin_audit_log_target_id"), table_name="admin_audit_log")
    op.drop_index(op.f("ix_admin_audit_log_action"), table_name="admin_audit_log")
    op.drop_index(op.f("ix_admin_audit_log_admin_id"), table_name="admin_audit_log")
    op.drop_table("admin_audit_log")

    op.drop_index(
        "ix_admin_refresh_tokens_admin_revoked", table_name="admin_refresh_tokens"
    )
    op.drop_index(
        op.f("ix_admin_refresh_tokens_admin_id"), table_name="admin_refresh_tokens"
    )
    op.drop_table("admin_refresh_tokens")

    op.drop_index(op.f("ix_admin_users_email"), table_name="admin_users")
    op.drop_table("admin_users")
