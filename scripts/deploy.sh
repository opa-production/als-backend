#!/usr/bin/env bash
# Server-side deploy. Run over SSH by .github/workflows/deploy.yml, never by
# hand in normal operation.
#
#     scripts/deploy.sh <commit-sha> [repo-url]
#
# Deploys an exact commit rather than "whatever origin/master is now", so a push
# that lands mid-deploy cannot ship a commit that was never tested.
#
# `repo-url` is the repository the workflow ran in. It is only used if the
# configured remote cannot be fetched — see "Reconciling the remote" below.
set -euo pipefail

APP_DIR=/opt/als-backend
HEALTH_URL=http://127.0.0.1:8000/health
SERVICE=als-backend

TARGET_SHA="${1:?usage: deploy.sh <commit-sha> [repo-url]}"
EXPECTED_REPO="${2:-}"

cd "$APP_DIR"

# The commit to fall back to if the new one will not serve traffic.
PREVIOUS_SHA="$(git rev-parse HEAD)"
echo "==> current $PREVIOUS_SHA -> target $TARGET_SHA"

# --- Reconciling the remote ---------------------------------------------------
#
# The remote configured at provision time can stop working without anything on
# this box changing. Two ways it has actually happened:
#
#   · The clone used an SSH URL (git@github.com:...), which needs a key
#     registered with GitHub. The key provision.sh generates is for the opposite
#     direction — GitHub Actions into this server — so `git fetch` fails with
#     "Permission denied (publickey)" that names github.com, not this host.
#
#   · The repository was renamed or transferred to another owner. Git follows
#     GitHub's redirect for a while, and then one day does not.
#
# So: try what is configured, and only if that fails fall back to the URL the
# workflow passed — which is by definition the repository that just ran the
# tests. Rewriting unconditionally would break a private repo that is correctly
# using an SSH deploy key.
REMOTE_URL="$(git remote get-url origin 2>/dev/null || echo '(none)')"
echo "==> fetching from $REMOTE_URL"

if ! git fetch --quiet origin 2>/tmp/als-deploy-fetch.log; then
    sed 's/^/    /' /tmp/als-deploy-fetch.log >&2

    if [ -z "$EXPECTED_REPO" ]; then
        echo "!! could not fetch from $REMOTE_URL, and no fallback URL was given." >&2
        echo "!! if this is an SSH remote on a public repository, switch it:" >&2
        echo "!!     git -C $APP_DIR remote set-url origin \\" >&2
        echo "!!         \"\$(git -C $APP_DIR remote get-url origin \\" >&2
        echo "!!           | sed -e 's#^git@github\\.com:#https://github.com/#')\"" >&2
        exit 1
    fi

    echo "==> that failed; falling back to $EXPECTED_REPO" >&2
    git remote set-url origin "$EXPECTED_REPO"

    if ! git fetch --quiet origin 2>/tmp/als-deploy-fetch.log; then
        sed 's/^/    /' /tmp/als-deploy-fetch.log >&2
        echo "!! could not fetch from $EXPECTED_REPO either." >&2
        echo "!! a private repository needs a read-only deploy key on this server:" >&2
        echo "!!     sudo -u als ssh-keygen -t ed25519 -N '' -f /home/als/.ssh/github_deploy" >&2
        echo "!!     cat /home/als/.ssh/github_deploy.pub" >&2
        echo "!! add that to the repo's Settings > Deploy keys, then point the" >&2
        echo "!! remote back at the git@github.com: form." >&2
        exit 1
    fi

    echo "==> remote repaired: origin is now $EXPECTED_REPO"
fi

# The commit has to exist after the fetch. Without this the `git reset` below
# fails with git's own terse message, which reads like a corrupt repository
# rather than "CI deployed a commit this remote does not have".
if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
    echo "!! $TARGET_SHA is not in $(git remote get-url origin) after fetching." >&2
    echo "!! nothing has been changed. This usually means the workflow and this" >&2
    echo "!! server are pointed at different repositories." >&2
    exit 1
fi

git reset --hard --quiet "$TARGET_SHA"

# --- The virtualenv -----------------------------------------------------------
#
# Built here if it is missing, rather than assumed. provision.sh creates it, but
# a box can end up without one for ordinary reasons: provisioning that stopped
# half way, a venv built against a Python that has since been removed, or a
# restore that skipped it because .venv is gitignored.
#
# Recreating is safe and idempotent — nothing in it is state. Everything it
# holds comes from pyproject.toml, and the next two lines rebuild it anyway.
if [ ! -x .venv/bin/pip ]; then
    if [ -e .venv ]; then
        echo "==> .venv exists but has no working pip -- rebuilding it"
        rm -rf .venv
    else
        echo "==> no virtualenv -- creating one"
    fi

    # Matches provision.sh. The fallback matters on a box where 3.12 is the
    # system python and the versioned name was never installed.
    VENV_PYTHON=python3.12
    command -v "$VENV_PYTHON" > /dev/null 2>&1 || VENV_PYTHON=python3

    if ! "$VENV_PYTHON" -m venv .venv; then
        echo "!! could not create a virtualenv with $VENV_PYTHON." >&2
        echo "!! the venv module is packaged separately on Debian and Ubuntu:" >&2
        echo "!!     sudo apt-get install -y ${VENV_PYTHON}-venv" >&2
        exit 1
    fi
fi

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
