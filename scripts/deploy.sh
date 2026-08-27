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
#: Restarted alongside the API so a deploy does not leave an old worker
#: parsing with last release's code. Failing to restart it is not fatal — the
#: API is what serves students.
WORKER=als-worker

#: The operator-managed settings file, outside the git tree so a bad checkout
#: can never expose it. systemd hands it to the service; this script has to
#: source it itself before running migrations.
ENV_FILE=/etc/als-backend/env

# --- Running from an immutable copy -------------------------------------------
#
# This script replaces itself part-way through: `git reset --hard` below rewrites
# scripts/deploy.sh while bash is still executing it.
#
# Bash reads a script incrementally rather than loading it whole, and it tracks
# its position as a *byte offset*. Swap the file underneath it and execution
# resumes at that offset in the new content — which is almost never the start of
# the line it was on. The result is a script that appears to skip its own code,
# or runs a fragment of a line, and reports a line number that does not match
# what is on disk. It is a genuinely confusing failure and worth ten lines to
# make impossible.
#
# Copying to /tmp first makes the executing text immutable for the run.
if [ -z "${ALS_DEPLOY_PINNED:-}" ]; then
    pinned="$(mktemp /tmp/als-deploy.XXXXXX)"
    cat "$0" > "$pinned"
    export ALS_DEPLOY_PINNED="$pinned"

    bash "$pinned" "$@"
    status=$?

    rm -f "$pinned"
    exit "$status"
fi

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
#     registered with GitHub. The deploy key travels the opposite
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

SCRIPT_BEFORE="$(git rev-parse "HEAD:scripts/deploy.sh" 2>/dev/null || echo none)"

git reset --hard --quiet "$TARGET_SHA"

# --- Picking up a change to this script ---------------------------------------
#
# The commit just checked out may contain a newer version of this very file, and
# without this the new version would not run until the *next* deploy. That is a
# genuinely confusing lag: a fix is pushed, CI goes green on the commit that
# contains it, the deploy fails anyway on the bug that was just fixed, and the
# error names a line number that does not exist in the file you are reading.
#
# So: if the checkout changed this script, hand over to the new one. Bounded to a
# single handover by ALS_DEPLOY_RELOADED, and the second run is cheap because the
# fetch and reset it repeats are both no-ops by then.
SCRIPT_AFTER="$(git rev-parse "HEAD:scripts/deploy.sh" 2>/dev/null || echo none)"

if [ "$SCRIPT_BEFORE" != "$SCRIPT_AFTER" ] && [ -z "${ALS_DEPLOY_RELOADED:-}" ]; then
    echo "==> this commit updates deploy.sh -- handing over to the new version"
    export ALS_DEPLOY_RELOADED=1

    # `exec` replaces this process, so the wrapper's cleanup never runs. Drop the
    # copy now instead. Safe while executing from it: the inode survives an
    # unlink until the last descriptor closes, which is what exec does anyway.
    stale_pin="${ALS_DEPLOY_PINNED:-}"
    # Unset so the new run pins its own copy rather than trusting this one.
    unset ALS_DEPLOY_PINNED
    if [ -n "$stale_pin" ]; then
        rm -f "$stale_pin"
    fi

    exec bash "$APP_DIR/scripts/deploy.sh" "$TARGET_SHA" "$EXPECTED_REPO"
fi

# --- The virtualenv -----------------------------------------------------------
#
# Built here if it is missing, rather than assumed. A box can end up without one
# for ordinary reasons: a server set up by hand that skipped the step, a venv
# built against a Python that has since been removed, or a restore that left it
# out because .venv is gitignored.
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

    # Every interpreter on the box gets a turn, rather than one guess.
    #
    # On Debian and Ubuntu, `python3.12 -m venv` fails when python3.12-venv is
    # not installed — the interpreter is present, `command -v` finds it, and the
    # failure only appears when ensurepip is actually needed. So the presence of
    # a python says nothing about whether it can build a venv, and the only way
    # to find out is to try.
    #
    # 3.12 first because pyproject requires >= 3.12;
    # then whatever `python3` points at; then 3.13 for a newer box.
    created=""
    tried=""

    for candidate in python3.12 python3 python3.13; do
        command -v "$candidate" > /dev/null 2>&1 || continue
        case " $tried " in *" $candidate "*) continue ;; esac
        tried="$tried $candidate"

        rm -rf .venv
        if "$candidate" -m venv .venv 2> /tmp/als-venv.log; then
            echo "==> virtualenv created with $candidate"
            created=1
            break
        fi

        echo "==> $candidate could not build one:"
        sed 's/^/    /' /tmp/als-venv.log >&2
    done

    # `virtualenv` carries its own copy of pip and so does not need ensurepip.
    # Where it happens to be installed it walks straight past the problem above.
    if [ -z "$created" ] && command -v virtualenv > /dev/null 2>&1; then
        echo "==> falling back to virtualenv, which bundles its own pip"
        rm -rf .venv
        if virtualenv --quiet --python=python3.12 .venv 2> /tmp/als-venv.log ||
            virtualenv --quiet .venv 2> /tmp/als-venv.log; then
            created=1
        else
            sed 's/^/    /' /tmp/als-venv.log >&2
        fi
    fi

    # Last resort, and it works without root.
    #
    # `--without-pip` never calls ensurepip, so it succeeds on exactly the box
    # that has just failed above: the venv module itself is in the standard
    # library, and Debian only strips *ensurepip* out into the separate
    # python3.x-venv package. So the environment can be built, and pip put into
    # it afterwards by PyPA's own installer.
    #
    # That is not a new trust boundary. The very next thing this script does is
    # `pip install .`, which downloads and executes setup code from PyPI —
    # get-pip.py comes from the same project, over the same TLS.
    #
    # Deliberately noisy. It leaves the server in a working state but still
    # misconfigured, and a silent workaround is one nobody ever goes back and
    # fixes.
    if [ -z "$created" ]; then
        for candidate in $tried; do
            rm -rf .venv
            "$candidate" -m venv --without-pip .venv 2> /dev/null || continue

            if curl --fail --silent --show-error --max-time 30 \
                https://bootstrap.pypa.io/get-pip.py -o /tmp/als-get-pip.py &&
                .venv/bin/python /tmp/als-get-pip.py --quiet; then
                echo ""
                echo "!! ---------------------------------------------------------"
                echo "!! This server is missing ${candidate}-venv."
                echo "!!"
                echo "!! The deploy worked around it by building the environment"
                echo "!! without pip and installing pip separately. That is slower"
                echo "!! on every deploy and depends on reaching pypa.io."
                echo "!!"
                echo "!! Fix it properly, once, as root:"
                echo "!!     sudo apt-get install -y ${candidate}-venv"
                echo "!! ---------------------------------------------------------"
                echo ""
                created=1
                break
            fi
        done
    fi

    if [ -z "$created" ]; then
        rm -rf .venv
        echo "" >&2
        echo "!! no interpreter on this server can build a virtualenv." >&2
        echo "!! tried:$tried" >&2
        echo "" >&2
        echo "!! On Debian and Ubuntu the venv module is a separate package." >&2
        echo "!! Install it once, as root, then re-run this deploy:" >&2
        echo "" >&2
        for candidate in $tried; do
            echo "!!     sudo apt-get install -y ${candidate}-venv" >&2
        done
        echo "" >&2
        # Said plainly because it looks like a gap in the automation and is not.
        echo "!! This deploy cannot install it itself, by design: the deploy" >&2
        echo "!! account's sudo is limited to restarting one service, so that a" >&2
        echo "!! leaked CI key is not equivalent to root. See DEPLOYMENT.md." >&2
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
#
# The settings file has to be loaded explicitly. The service gets it from
# systemd's EnvironmentFile, but this script is a plain SSH session and gets
# nothing. For a long time that went unnoticed because a stray .env sat in the
# checkout and pydantic-settings picked it up from the working directory --
# so migrations were quietly running against whatever *that* file said. When
# the .env was removed, DATABASE_URL fell back to the built-in default and
# alembic tried localhost:5432, on a box whose database is in another country.
#
# Read in a subshell-free block with `set -a` so exported values reach alembic,
# then turned straight back off. `set +u` around the source is deliberate: the
# file is operator-edited and a stray bare word should not abort the deploy
# under `set -u`.
if [ -r "$ENV_FILE" ]; then
    set -a
    set +u
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set -u
    set +a
else
    echo "!! $ENV_FILE is not readable by $(id -un)." >&2
    echo "" >&2
    echo "!! Migrations would otherwise run against the built-in default" >&2
    echo "!! connection string -- localhost -- which is not this project's" >&2
    echo "!! database. Refusing rather than guessing." >&2
    echo "" >&2
    echo "!! The file is root-owned by design. Give this account read access" >&2
    echo "!! through the group rather than making it world-readable:" >&2
    echo "" >&2
    echo "!!     sudo chown root:$(id -gn) $ENV_FILE" >&2
    echo "!!     sudo chmod 640 $ENV_FILE" >&2
    exit 1
fi

.venv/bin/alembic upgrade head

# --- Restarting without root --------------------------------------------------
#
# The clean path is `sudo systemctl restart`, which the server grants this account
# for exactly two units. On a box where that sudoers rule was never written, the
# deploy would otherwise stop here having already installed the new code and run
# the migrations — everything done except the one step that makes any of it take
# effect.
#
# So there is a second route, and it is safe only under conditions this checks
# rather than assumes:
#
#   · Restart=always — systemd will bring the service straight back. Under any
#     other policy, signalling the process would simply take production down,
#     which is far worse than a failed deploy.
#   · User= matches whoever is running this, so the process is ours to signal.
#     `kill` on a root-owned process would fail anyway; checking first means the
#     reason is stated rather than discovered.
#   · A real MainPID, so the unit is actually running.
#
# Reading unit properties needs no privileges; only acting on them does.
restart_worker() {
    # Best effort, and never fatal. The worker only reads a queue: an old one
    # left running parses with last release's code, which is a stale extraction
    # rather than a broken product. The API is what students talk to, and
    # failing a deploy that successfully shipped it would be the wrong trade.
    if sudo -n /bin/systemctl restart "$WORKER" 2> /dev/null; then
        echo "==> restarted $WORKER"
    else
        echo "==> could not restart $WORKER (not fatal; it will pick up the new"
        echo "    code at its next restart)"
    fi
}

restart_service() {
    if sudo -n /bin/systemctl restart "$SERVICE" 2> /tmp/als-restart.log; then
        return 0
    fi

    sed 's/^/    /' /tmp/als-restart.log >&2
    echo "==> no sudo rule for restarting $SERVICE -- trying a signal instead"

    local policy main_pid unit_user
    policy="$(systemctl show "$SERVICE" -p Restart --value 2> /dev/null || true)"
    main_pid="$(systemctl show "$SERVICE" -p MainPID --value 2> /dev/null || echo 0)"
    unit_user="$(systemctl show "$SERVICE" -p User --value 2> /dev/null || true)"

    if [ "$policy" != "always" ]; then
        echo "!! $SERVICE has Restart=${policy:-unset}, so signalling it would" >&2
        echo "!! stop the service rather than restart it. Not doing that." >&2
        return 1
    fi

    if [ "${main_pid:-0}" -le 0 ]; then
        echo "!! $SERVICE is not running, so there is nothing to signal." >&2
        return 1
    fi

    if [ "$unit_user" != "$(id -un)" ]; then
        echo "!! $SERVICE runs as ${unit_user:-root}, not $(id -un)." >&2
        return 1
    fi

    echo "==> Restart=always and the process is ours -- signalling pid $main_pid"
    # SIGTERM, not SIGKILL: the unit sets TimeoutStopSec=30 so in-flight
    # requests get to finish, and Restart=always brings it back after
    # RestartSec. The health loop below is what confirms it actually did.
    kill -TERM "$main_pid" 2> /dev/null || return 1
    return 0
}

echo "==> restarting $SERVICE"
if ! restart_service; then
    echo "" >&2
    echo "!! The new code is installed and the migrations have run, but the" >&2
    echo "!! service is still serving the old build." >&2
    echo "" >&2
    echo "!! Grant this account permission to restart the service, once, as root." >&2
    echo "!! Two exact commands, not NOPASSWD: ALL -- the CI deploy key can reach" >&2
    echo "!! this account, so a blanket rule would make that key equivalent to root:" >&2
    echo "" >&2
    echo "!!     printf '$(id -un) ALL=(root) NOPASSWD: /bin/systemctl restart $SERVICE\\n' \\" >&2
    echo "!!       | sudo tee /etc/sudoers.d/$SERVICE" >&2
    echo "!!     printf '$(id -un) ALL=(root) NOPASSWD: /bin/systemctl restart $WORKER\\n' \\" >&2
    echo "!!       | sudo tee -a /etc/sudoers.d/$SERVICE" >&2
    echo "!!     sudo chmod 440 /etc/sudoers.d/$SERVICE" >&2
    echo "" >&2
    echo "!! The full list of what this box needs is in DEPLOYMENT.md section 2." >&2
    exit 1
fi

# A restart that "succeeded" tells you systemd forked a process, not that the
# app can serve. Without this gate a deploy that dies on a bad env var goes
# green in CI and red in production.
echo "==> waiting for health"
for attempt in $(seq 1 30); do
    if curl --fail --silent --max-time 2 "$HEALTH_URL" > /dev/null; then
        echo "==> healthy after ${attempt}s"
        restart_worker
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
restart_service

echo "!! rolled back. Inspect with: journalctl -u $SERVICE -n 100 --no-pager" >&2
exit 1
