#!/usr/bin/env sh
# Run by the platform BEFORE new containers take traffic — never from the app's
# lifespan. Migrating on startup means every container in a rolling deploy
# races the same DDL, and the loser crash-loops.
set -eu

echo "==> alembic upgrade head"
alembic upgrade head
echo "==> migrations applied"
