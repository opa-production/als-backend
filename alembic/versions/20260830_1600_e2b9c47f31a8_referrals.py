"""referrals

Revision ID: e2b9c47f31a8
Revises: d7a4b62e19c3
Created: 2026-08-30 16:00:00.000000+00:00

One new table and two nullable columns on ``users``. Both columns are nullable
with no default and no backfill, so the release still running does not know
they exist and a rolling deploy is safe.

``users.referral_code`` is unique but empty for every existing row. That is
deliberate: a code is minted the first time a student opens the referral
screen. Backfilling millions of codes nobody asked for is a migration that can
fail on a collision, for a column most accounts never read.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2b9c47f31a8"
down_revision: str | None = "d7a4b62e19c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("referral_code", sa.String(length=12), nullable=True)
    )
    op.add_column(
        "users", sa.Column("referred_by_user_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        op.f("ix_users_referral_code"), "users", ["referral_code"], unique=True
    )
    op.create_index(
        op.f("ix_users_referred_by_user_id"), "users", ["referred_by_user_id"]
    )
    op.create_foreign_key(
        op.f("fk_users_referred_by_user_id_users"),
        "users",
        "users",
        ["referred_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "referral_rewards",
        sa.Column("referrer_id", sa.Uuid(), nullable=False),
        sa.Column("referred_user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("friend_days", sa.Integer(), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("vest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("banked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
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
            ["referrer_id"],
            ["users.id"],
            name=op.f("fk_referral_rewards_referrer_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["referred_user_id"],
            ["users.id"],
            name=op.f("fk_referral_rewards_referred_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_referral_rewards")),
        sa.UniqueConstraint("referred_user_id", name="referral_rewards_referred"),
    )
    op.create_index(
        op.f("ix_referral_rewards_referrer_id"), "referral_rewards", ["referrer_id"]
    )
    op.create_index(
        op.f("ix_referral_rewards_referred_user_id"),
        "referral_rewards",
        ["referred_user_id"],
    )
    op.create_index(
        "ix_referral_rewards_status_vest",
        "referral_rewards",
        ["status", "vest_at"],
    )
    op.create_index(
        "ix_referral_rewards_referrer_created",
        "referral_rewards",
        ["referrer_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_referral_rewards_referrer_created", table_name="referral_rewards"
    )
    op.drop_index("ix_referral_rewards_status_vest", table_name="referral_rewards")
    op.drop_index(
        op.f("ix_referral_rewards_referred_user_id"), table_name="referral_rewards"
    )
    op.drop_index(
        op.f("ix_referral_rewards_referrer_id"), table_name="referral_rewards"
    )
    op.drop_table("referral_rewards")

    op.drop_constraint(
        op.f("fk_users_referred_by_user_id_users"), "users", type_="foreignkey"
    )
    op.drop_index(op.f("ix_users_referred_by_user_id"), table_name="users")
    op.drop_index(op.f("ix_users_referral_code"), table_name="users")
    op.drop_column("users", "referred_by_user_id")
    op.drop_column("users", "referral_code")
