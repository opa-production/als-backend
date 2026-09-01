"""payment providers

Revision ID: c73a5b0e948d
Revises: b6d2e19f84c3
Created: 2026-09-01 12:00:00.000000+00:00

Three columns on ``payments``, for paying with more than one provider.

``provider`` is backfilled to ``kora`` and is the only one that has to be:
every row already in the table was a Kora charge, and the column decides which
API ``/billing/verify`` and the console's reconcile button ask about a
reference. Left NULL, an old row would be reconciled against whichever provider
happened to be the default, and "no such transaction" reads exactly like "they
never paid".

It carries a server default as well as the backfill. The release still running
does not know the column exists and will keep inserting without it, so a NOT
NULL column with no server default would make every payment fail for the length
of the deploy.

``checkout_request_id`` is Safaricom's handle for one STK prompt, and the index
on it is not optional: it is the lookup on an unauthenticated callback endpoint,
which is exactly where a table scan is a denial-of-service anyone can trigger.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c73a5b0e948d"
down_revision: str | None = "b6d2e19f84c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column(
            "provider",
            sa.String(length=16),
            nullable=False,
            server_default="kora",
        ),
    )
    op.add_column(
        "payments",
        sa.Column("checkout_request_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "payments", sa.Column("receipt", sa.String(length=32), nullable=True)
    )

    # Where the M-Pesa callback looks a payment up, and the only thing standing
    # between that open endpoint and a sequential scan of every payment ever
    # taken.
    op.create_index(
        op.f("ix_payments_checkout_request_id"),
        "payments",
        ["checkout_request_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payments_checkout_request_id"), table_name="payments")
    op.drop_column("payments", "receipt")
    op.drop_column("payments", "checkout_request_id")
    op.drop_column("payments", "provider")
