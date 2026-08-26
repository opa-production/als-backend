#!/usr/bin/env bash
# One-time Contabo/Ubuntu VPS setup for the ALS API. Run once, as root:
#
#     sudo bash provision.sh --domain api.ardena.co.ke \
#                            --repo https://github.com/opa-production/als-backend.git
#
# Use the HTTPS URL for a public repository. The SSH form needs a key
# registered with GitHub, and the key generated below is for the opposite
# direction — GitHub Actions into this server — so `git fetch` in deploy.sh
# would fail with a "Permission denied (publickey)" that names github.com.
#
# For a private repository, clone over SSH and add a read-only deploy key:
#     sudo -u als ssh-keygen -t ed25519 -N '' -f /home/als/.ssh/github_deploy
#     cat /home/als/.ssh/github_deploy.pub    # -> repo Settings > Deploy keys
#
# Safe to re-run: every step checks before it acts. Re-run it after changing
# deploy/als-backend.service or deploy/nginx.conf to push those out.
#
# What it does NOT do: fill in /etc/als-backend/env (the secrets are yours to
# paste) or obtain the TLS certificate (certbot needs DNS to resolve first).
# It prints both as next steps.
set -euo pipefail

APP_USER=als
APP_DIR=/opt/als-backend
ENV_DIR=/etc/als-backend
ENV_FILE="$ENV_DIR/env"
SERVICE=als-backend
PYTHON=python3.12

DOMAIN=""
REPO=""
BRANCH=master

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --repo)   REPO="$2";   shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }
[ -n "$DOMAIN" ] || { echo "--domain is required" >&2; exit 2; }
[ -n "$REPO" ]   || { echo "--repo is required" >&2; exit 2; }

say() { printf '\n==> %s\n' "$1"; }


say "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq software-properties-common curl git ca-certificates

# Ubuntu 24.04 ships Python 3.12; 22.04 stops at 3.10 and pyproject.toml
# requires >=3.12, so the PPA is added only where it is actually needed.
if ! command -v "$PYTHON" > /dev/null; then
    say "python3.12 not present -- adding the deadsnakes PPA"
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
fi
apt-get install -y -qq "$PYTHON" "$PYTHON-venv" "$PYTHON-dev" build-essential
apt-get install -y -qq nginx certbot python3-certbot-nginx ufw


say "creating the $APP_USER user"
if ! id "$APP_USER" > /dev/null 2>&1; then
    # A real shell and home directory: this account is also the one GitHub
    # Actions logs in as to deploy.
    useradd --create-home --shell /bin/bash "$APP_USER"
fi


say "preparing $APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone --branch "$BRANCH" "$REPO" "$APP_DIR"
else
    echo "    already a git checkout, leaving it alone"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"


say "building the virtualenv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    sudo -u "$APP_USER" "$PYTHON" -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"


say "creating $ENV_FILE"
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'ENVTEMPLATE'
# systemd EnvironmentFile. One KEY=value per line, no `export`, no shell
# quoting. If the database password contains a character that is special in a
# URL (@ : / ? # &) it must be percent-encoded inside DATABASE_URL.

ENVIRONMENT=production
DEBUG=false

# --- Supabase Postgres --------------------------------------------------
# Use the SESSION pooler string from Supabase (Project Settings > Database >
# Connection string > Session mode: port 5432 on *.pooler.supabase.com).
#
# Not the direct db.<ref>.supabase.co host: that is IPv6-only unless you pay
# for the IPv4 add-on, and it will simply fail to connect from this VPS.
#
# Not the transaction pooler (port 6543) either: this is a long-lived server
# with its own connection pool, which is what session mode exists for. If you
# do move to 6543, set DATABASE_USE_PGBOUNCER=true or asyncpg starts throwing
# InvalidSQLStatementNameError under load and never in testing.
DATABASE_URL=postgresql://postgres.CHANGEME:CHANGEME@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?ssl=require
DATABASE_USE_PGBOUNCER=false
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20

# --- Auth ---------------------------------------------------------------
# Generate with: openssl rand -hex 32
# Changing this logs every user out.
JWT_SECRET=CHANGEME
JWT_ACCESS_TTL_MINUTES=30
JWT_REFRESH_TTL_DAYS=60

# --- Supabase Storage ---------------------------------------------------
# The service_role key, not the anon key. Required: the app refuses to start
# in production without it.
SUPABASE_URL=https://CHANGEME.supabase.co
SUPABASE_SERVICE_KEY=CHANGEME
SUPABASE_STORAGE_SIGNED_URL_TTL=3600

# --- SMS (Celcom Africa) ------------------------------------------------
# Blank means OTP codes go to the journal instead of being sent:
#   journalctl -u als-backend -f
SMS_API_KEY=
SMS_PARTNER_ID=
SMS_SENDER_ID=

# --- Google sign-in -----------------------------------------------------
GOOGLE_CLIENT_IDS=

# --- OTP ----------------------------------------------------------------
OTP_TTL_SECONDS=600
OTP_MAX_ATTEMPTS=5
OTP_MAX_SENDS_PER_HOUR=5

# --- Payments (Kora) ----------------------------------------------------
# https://korahq.com. Blank means /billing/checkout and /billing/verify report
# that payments are unavailable; nothing else is affected.
#
# Kora charges the MAJOR unit (350 = KES 350) and signs webhooks over only the
# `data` object with SHA-256. Neither needs configuring -- both are worth
# knowing before anyone "fixes" one of them.
KORA_SECRET_KEY=
KORA_PUBLIC_KEY=
# Leave blank: Kora signs webhooks with the secret key above.
KORA_WEBHOOK_SECRET=
# Blank uses the Kora dashboard's redirect. als://billing returns to the app.
KORA_CALLBACK_URL=
RECEIPT_EMAIL_DOMAIN=__DOMAIN__

# --- This service's own address -----------------------------------------
# Where Kora is told to post webhooks. Cannot be derived from a request:
# behind nginx the app only ever sees 127.0.0.1:8000.
PUBLIC_BASE_URL=https://__DOMAIN__

# --- Outbound -----------------------------------------------------------
HTTP_TIMEOUT_SECONDS=15

# --- CORS ---------------------------------------------------------------
# The mobile app does not use CORS. Only web origins belong here -- the admin
# console is the one that matters, and missing it makes every request from the
# console fail in the browser before it reaches this service.
CORS_ORIGINS=https://admin.__DOMAIN__

# --- Process ------------------------------------------------------------
# Two per core is the usual starting point for an IO-bound service.
WEB_CONCURRENCY=2
ENVTEMPLATE

    # The heredoc above is quoted, so nothing in it was expanded -- which is
    # what keeps a `$` inside a future password from being eaten. The three
    # placeholders are filled in here instead, the same way deploy/nginx.conf
    # is handled further down.
    sed -i "s/__DOMAIN__/$DOMAIN/g" "$ENV_FILE"
    echo "    written -- replace every CHANGEME before starting the service"
else
    echo "    already exists, leaving it alone"
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"


say "installing the systemd unit"
install -m 644 "$APP_DIR/deploy/als-backend.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" > /dev/null


say "granting $APP_USER permission to restart the service"
# Exactly one command, no password. The deploy needs this and nothing else; a
# blanket NOPASSWD:ALL would make the CI deploy key equivalent to root.
printf '%s ALL=(root) NOPASSWD: /bin/systemctl restart %s\n' "$APP_USER" "$SERVICE" \
    > "/etc/sudoers.d/$SERVICE"
chmod 440 "/etc/sudoers.d/$SERVICE"
visudo -cf "/etc/sudoers.d/$SERVICE" > /dev/null


say "configuring nginx for $DOMAIN"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/deploy/nginx.conf" \
    > "/etc/nginx/sites-available/$SERVICE"
ln -sf "/etc/nginx/sites-available/$SERVICE" "/etc/nginx/sites-enabled/$SERVICE"
# The stock default site also listens on 80 and would win on a bare IP.
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx


say "configuring the firewall"
ufw allow OpenSSH > /dev/null
ufw allow 'Nginx Full' > /dev/null
ufw --force enable > /dev/null
ufw status


say "preparing the CI deploy key"
SSH_HOME="/home/$APP_USER/.ssh"
CI_KEY="$SSH_HOME/github_actions"
sudo -u "$APP_USER" mkdir -p "$SSH_HOME"
chmod 700 "$SSH_HOME"
if [ ! -f "$CI_KEY" ]; then
    sudo -u "$APP_USER" ssh-keygen -t ed25519 -N "" -C "github-actions@$DOMAIN" -f "$CI_KEY" > /dev/null
    sudo -u "$APP_USER" tee -a "$SSH_HOME/authorized_keys" < "$CI_KEY.pub" > /dev/null
    chmod 600 "$SSH_HOME/authorized_keys"
fi
# Pin GitHub's host key so the deploy's `git fetch` is neither prompted nor
# spoofable.
sudo -u "$APP_USER" touch "$SSH_HOME/known_hosts"
if ! sudo -u "$APP_USER" ssh-keygen -F github.com > /dev/null 2>&1; then
    ssh-keyscan -t ed25519 github.com 2>/dev/null \
        | sudo -u "$APP_USER" tee -a "$SSH_HOME/known_hosts" > /dev/null
fi

PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo '<this server IP>')"

echo ""
echo "============================================================"
echo " Provisioning done. Three things left, in this order:"
echo ""
echo " 1. Fill in the secrets, then start the service:"
echo "        sudo nano $ENV_FILE"
echo "        sudo systemctl start $SERVICE"
echo "        systemctl status $SERVICE"
echo ""
echo " 2. Point $DOMAIN at $PUBLIC_IP with a DNS A record."
echo "    Once it resolves, get the certificate:"
echo "        sudo certbot --nginx -d $DOMAIN"
echo ""
echo " 3. Add these GitHub Actions repository secrets"
echo "    (Settings > Secrets and variables > Actions):"
echo "        SSH_HOST = $PUBLIC_IP"
echo "        SSH_USER = $APP_USER"
echo "        SSH_KEY  = the full output of"
echo "                       sudo cat $CI_KEY"
echo ""
echo "    If the repository is private, also add this as a"
echo "    deploy key (repo Settings > Deploy keys, read-only):"
sed 's/^/        /' "$CI_KEY.pub"
echo "============================================================"
