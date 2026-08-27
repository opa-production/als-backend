#!/usr/bin/env bash
#
# Run this ON THE VPS, as the `als` user, to find out what is actually wrong.
#
#     ssh als@als.ardena.xyz 'bash -s' < scripts/doctor.sh
#
# or, if the checkout is already there:
#
#     sudo -u als bash /opt/als-backend/scripts/doctor.sh
#
# Read-only. It changes nothing, needs no sudo, and prints a fix for every
# problem it finds. It exists because this project spent a long stretch of
# deploys failing one step further along each time, when a single pass like
# this would have listed every cause at once.

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/als-backend}"
VENV="$APP_DIR/.venv"
ENV_FILE="${ENV_FILE:-/etc/als-backend/env}"
SERVICE="als-backend"
WORKER="als-worker"

problems=0
ok()    { printf '  ok    %s\n' "$1"; }
bad()   { printf '  FAIL  %s\n' "$1"; problems=$((problems + 1)); }
warn()  { printf '  warn  %s\n' "$1"; }
fix()   { printf '        -> %s\n' "$1"; }
info()  { printf '  info  %s\n' "$1"; }
title() { printf '\n%s\n' "$1"; }

printf '=== ALS server check -- %s on %s ===\n' "$(id -un)" "$(hostname)"

# ---------------------------------------------------------------- interpreter
title "Python"
py=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" > /dev/null 2>&1; then
        py="$candidate"
        ok "$candidate is $("$candidate" -V 2>&1)"
        break
    fi
done
[ -n "$py" ] || bad "no python3 on PATH"

if [ -n "$py" ]; then
    # The venv module is a separate package on Debian and Ubuntu. This is the
    # check that would have saved a whole deploy cycle.
    if "$py" -c 'import ensurepip' 2> /dev/null; then
        ok "$py can build a virtualenv"
    else
        bad "$py has no ensurepip -- 'python -m venv' will fail"
        fix "sudo apt-get install -y ${py}-venv"
    fi
fi

# ------------------------------------------------------------------- checkout
title "Checkout at $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    ok "git checkout present"

    owner=$(stat -c '%U' "$APP_DIR" 2> /dev/null || echo "?")
    if [ "$owner" = "als" ]; then
        ok "owned by als"
    else
        bad "owned by $owner, but the units run as als"
        fix "sudo chown -R als:als $APP_DIR"
    fi

    remote=$(git -C "$APP_DIR" remote get-url origin 2> /dev/null || echo "")
    case "$remote" in
        https://*)
            ok "remote is HTTPS ($remote)"
            ;;
        git@* | ssh://*)
            bad "remote is SSH ($remote) -- needs a key registered with GitHub"
            fix "sudo -u als git -C $APP_DIR remote set-url origin https://github.com/opa-production/als-backend.git"
            ;;
        "")
            bad "no 'origin' remote"
            ;;
        *)
            warn "unusual remote: $remote"
            ;;
    esac

    info "HEAD is $(git -C "$APP_DIR" rev-parse --short HEAD 2> /dev/null || echo '?')"
else
    bad "no checkout at $APP_DIR"
    fix "sudo -u als git clone https://github.com/opa-production/als-backend.git $APP_DIR"
fi

# ----------------------------------------------------------------- virtualenv
title "Virtualenv at $VENV"
if [ -x "$VENV/bin/python" ]; then
    ok "interpreter present -- $("$VENV/bin/python" -V 2>&1)"

    if [ -x "$VENV/bin/pip" ]; then
        ok "pip present"
    else
        bad "no pip in the venv -- deploy.sh fails at 'installing dependencies'"
        fix "sudo -u als $VENV/bin/python -m ensurepip --upgrade"
    fi

    if (cd "$APP_DIR" 2> /dev/null && "$VENV/bin/python" -c 'import app.main') 2> /dev/null; then
        ok "the app imports"
    else
        bad "the app does not import from this venv -- dependencies missing or broken"
        fix "sudo -u als $VENV/bin/pip install $APP_DIR"
    fi
else
    bad "no virtualenv -- nothing here can run the app"
    fix "sudo -u als ${py:-python3.12} -m venv $VENV"
    fix "sudo -u als $VENV/bin/pip install $APP_DIR"
fi

# -------------------------------------------------------------------- secrets
title "Secrets at $ENV_FILE"
if [ -r "$ENV_FILE" ]; then
    # Readable by us means readable too widely: it should be root-only 0600.
    warn "readable by $(id -un) -- it should be root-owned and 0600"
    fix "sudo chown root:root $ENV_FILE && sudo chmod 600 $ENV_FILE"
elif [ -e "$ENV_FILE" ]; then
    ok "present and not readable by this account (correct)"
else
    bad "missing -- the units will not start"
    fix "sudo install -d -m 755 $(dirname "$ENV_FILE")"
    fix "sudo install -m 600 /dev/null $ENV_FILE   # then fill it from .env.example"
fi

# Values are read through systemd, which can see the file even when this
# account cannot. Never print a value -- only whether it is set.
title "Configuration the service actually loaded"
if env_dump=$(systemctl show "$SERVICE" -p Environment --value 2> /dev/null); then
    if [ -z "$env_dump" ]; then
        warn "systemd reports no environment for $SERVICE (is it installed?)"
    fi
    for key in ENVIRONMENT JWT_SECRET DATABASE_URL DEEPSEEK_API_KEY KORA_SECRET_KEY; do
        val=$(printf '%s' "$env_dump" | tr ' ' '\n' | grep "^${key}=" | cut -d= -f2- || true)
        case "$key:$val" in
            ENVIRONMENT:production)
                ok "ENVIRONMENT=production"
                ;;
            ENVIRONMENT:)
                bad "ENVIRONMENT is not set"
                fix "add ENVIRONMENT=production to $ENV_FILE"
                ;;
            ENVIRONMENT:*)
                bad "ENVIRONMENT=$val -- /docs and /redoc are public"
                fix "set ENVIRONMENT=production in $ENV_FILE"
                ;;
            *:)
                bad "$key is not set"
                ;;
            *)
                ok "$key is set"
                ;;
        esac
    done
fi

# ---------------------------------------------------------------------- units
title "Services"
for unit in "$SERVICE" "$WORKER"; do
    if ! systemctl list-unit-files "$unit.service" > /dev/null 2>&1 \
        || ! systemctl list-unit-files "$unit.service" 2> /dev/null | grep -q "$unit"; then
        bad "$unit is not installed"
        fix "sudo install -m 644 $APP_DIR/deploy/$unit.service /etc/systemd/system/"
        fix "sudo systemctl daemon-reload && sudo systemctl enable --now $unit"
        continue
    fi

    state=$(systemctl is-active "$unit" 2> /dev/null || true)
    pid=$(systemctl show "$unit" -p MainPID --value 2> /dev/null || echo 0)

    if [ "$state" = "active" ]; then
        ok "$unit is active (pid $pid)"

        # The question this whole script was written for: is the thing that is
        # serving traffic the same thing the deploy updates?
        exe=""
        if [ "$pid" != "0" ] && [ -r "/proc/$pid/cmdline" ]; then
            exe=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2> /dev/null || true)
        fi
        case "$exe" in
            "$VENV"/*)
                ok "  running from $VENV -- deploys reach it"
                ;;
            "")
                warn "  cannot read /proc/$pid/cmdline from this account"
                ;;
            *)
                bad "  running: $exe"
                fix "NOT from $VENV -- deploys update code this process never loads"
                ;;
        esac
    else
        bad "$unit is $state"
        fix "sudo systemctl status $unit --no-pager -n 30"
    fi
done

# -------------------------------------------------------------------- sudoers
title "Restart permission"
if sudo -n /bin/systemctl restart "$SERVICE" --dry-run > /dev/null 2>&1; then
    ok "$(id -un) can restart $SERVICE without a password"
else
    bad "no passwordless restart -- every deploy stops after the migrations"
    fix "printf '$(id -un) ALL=(root) NOPASSWD: /bin/systemctl restart $SERVICE\\n' | sudo tee /etc/sudoers.d/$SERVICE"
    fix "printf '$(id -un) ALL=(root) NOPASSWD: /bin/systemctl restart $WORKER\\n' | sudo tee -a /etc/sudoers.d/$SERVICE"
    fix "sudo chmod 440 /etc/sudoers.d/$SERVICE && sudo visudo -cf /etc/sudoers.d/$SERVICE"
fi

# --------------------------------------------------------------- reachability
title "Reachability"
if command -v curl > /dev/null 2>&1; then
    # Not `... || echo 000`: on a failure curl has usually already printed a
    # code, and appending to it produces nonsense like "000000".
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
        http://127.0.0.1:8000/health 2> /dev/null)
    code=${code:-000}
    if [ "$code" = "200" ]; then
        ok "the app answers on 127.0.0.1:8000/health"
    else
        bad "127.0.0.1:8000/health returned $code -- nginx has nothing to proxy to"
    fi
else
    warn "no curl, skipping the local health check"
fi

if [ -e /etc/nginx/sites-enabled/als-backend ]; then
    ok "nginx site is enabled"
else
    bad "nginx site is not enabled"
    fix "see DEPLOYMENT.md section 2"
fi

# -------------------------------------------------------------------- verdict
title "-----"
if [ "$problems" -eq 0 ]; then
    printf 'Nothing broken. A push to master should deploy cleanly.\n'
else
    printf '%d problem(s). Each FAIL above carries the command that fixes it.\n' "$problems"
fi

# Always exits 0: this is a report, not a gate. Nothing should fail a pipeline
# because someone ran the doctor.
exit 0
