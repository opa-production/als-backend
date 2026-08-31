"""pdf page pool periods

Revision ID: f4c1d83a52b7
Revises: e2b9c47f31a8
Created: 2026-08-31 09:00:00.000000+00:00

No schema change. ``usage_counters`` already stores an arbitrary
``(metric, period_key)`` pair, so the page pool moving from a lifetime meter to
a monthly one plus a lifetime companion needs no new column — only that the
rows already written land under the name the new code reads them by.

Every existing ``pdf_pages`` row has ``period_key = 'lifetime'``, because that
is the only period the metric ever had. Those rows are renamed to
``pdf_pages_lifetime``, which is exactly what they were measuring: total pages
ever extracted for that account.

Renaming rather than leaving them is the point. Left alone they would simply
stop being read, and every free account that had already spent its hundred
pages would silently get a second hundred — the one ceiling that bounds what a
never-converting account can cost us, reset for the whole existing base by a
deploy.

Nothing is written for the new monthly ``pdf_pages`` metric. The absence of a
row *is* zero, so every account starts the current month with a full pool,
which is the intended and generous reading of the change.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4c1d83a52b7"
down_revision: str | None = "e2b9c47f31a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE usage_counters
               SET metric = 'pdf_pages_lifetime'
             WHERE metric = 'pdf_pages'
               AND period_key = 'lifetime'
            """
        )
    )


def downgrade() -> None:
    # The monthly rows written since the upgrade have no place in the old
    # scheme -- the old code reads one lifetime total and nothing else -- so
    # they are dropped rather than folded in. Folding them in would double-count
    # every page that was already counted under the lifetime row beside them.
    op.execute(
        sa.text("DELETE FROM usage_counters WHERE metric = 'pdf_pages'")
    )
    op.execute(
        sa.text(
            """
            UPDATE usage_counters
               SET metric = 'pdf_pages'
             WHERE metric = 'pdf_pages_lifetime'
            """
        )
    )
