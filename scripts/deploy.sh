#!/usr/bin/env bash
# Server-side deploy. Run over SSH by .github/workflows/deploy.yml, never by
# hand in normal operation.
#
#     scripts/deploy.sh <commit-sha>
#
# Deploys an exact commit rather than "whatever origin/master is now", so a push
# that lands mid-deploy cannot ship a commit that was never tested.
set -euo pipefail

APP_DIR=/opt/als-backend
HEALTH_URL=http://127.0.0.1:8000/health
SERVICE=als-backend

TARGET_SHA="${1:?usage: deploy.sh <commit-sha>}"

cd "$APP_DIR"

# The commit to fall back to if the new one will not serve traffic.
PREVIOUS_SHA="$(git rev-parse HEAD)"
echo "==> current $PREVIOUS_SHA -> target $TARGET_SHA"

git fetch --quiet origin
git reset --hard --quiet "$TARGET_SHA"

echo "==> installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet .

echo "==> alembic upgrade head"
# Migrations run before the restart, while the old code is still serving. That
# is only safe for additive changes -- a column dropped here breaks the running
# process instantly. See the note in alembic/script.py.mako.
.venv/bin/alembic upgrade head

echo "==> restarting $SERVICE"
sudo /bin/systemctl restart "$SERVICE"

# A restart that "succeeded" tells you systemd forked a process, not that the
# app can serve. Without this gate a deploy that dies on a bad env var goes
# green in CI and red in production.
echo "==> waiting for health"
for attempt in $(seq 1 30); do
    if curl --fail --silent --max-time 2 "$HEALTH_URL" > /dev/null; then
        echo "==> healthy after ${attempt}s"
        echo "==> deployed $TARGET_SHA"
        exit 0
    fi
    sleep 1
done

echo "!! health check failed after 30s -- rolling back to $PREVIOUS_SHA" >&2
git reset --hard --quiet "$PREVIOUS_SHA"
.venv/bin/pip install --quiet .
# Deliberately no `alembic downgrade`: an automatic schema rollback is far more
# dangerous than a failed deploy. Migrations here are additive, so the old code
# runs fine against the new schema. Un-pick the migration by hand if it is the
# thing that broke.
sudo /bin/systemctl restart "$SERVICE"

echo "!! rolled back. Inspect with: journalctl -u $SERVICE -n 100 --no-pager" >&2
exit 1
