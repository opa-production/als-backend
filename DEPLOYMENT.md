# Deployment

Production runs on a Contabo VPS behind nginx, against Supabase Postgres and
Supabase Storage. There is no Docker in this path — the service is a systemd
unit running uvicorn out of a virtualenv.

Pushing to `master` deploys automatically once the tests pass.

```
GitHub push  ->  Actions: ruff + pytest + migration drift check
                   |  (only on master, only if green)
                   v
             ssh als@vps  ->  scripts/deploy.sh <sha>
                                git reset --hard <sha>
                                pip install .
                                alembic upgrade head
                                systemctl restart als-backend
                                curl /health  ->  rollback if it never answers
```

---

## One-time setup

### 1. Supabase

Create the project, then take the **session pooler** connection string from
*Project Settings → Database → Connection string → Session mode*. It looks like:

```
postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

Three ways to get this wrong, all of which fail in ways that are hard to read:

- **Do not use the direct `db.<ref>.supabase.co` host.** It is IPv6-only unless
  you buy the IPv4 add-on, and the VPS will just time out connecting.
- **Do not use the transaction pooler on port 6543** unless you also set
  `DATABASE_USE_PGBOUNCER=true`. Transaction mode multiplexes one server
  connection across clients, so asyncpg's prepared-statement cache gets replayed
  on a connection that never prepared it — an `InvalidSQLStatementNameError`
  that appears only under load. Session mode (5432) is the right fit for a
  long-lived server that keeps its own pool.
- **Percent-encode the password** if it contains `@ : / ? # &`.

Append `?ssl=require`. Also collect the project URL and the **`service_role`**
key (not `anon`) from *Project Settings → API* — the app refuses to start in
production without them.

### 2. The VPS

The server is set up and maintained by hand. There is no provisioning script in
this repo, deliberately: a script that writes sudoers files, nginx sites and SSH
keys fights whatever is already on a box somebody else built, and the two ways
of doing it drift.

What follows is not instructions to run blindly — it is the list of things
`scripts/deploy.sh` and the systemd units *assume are already true*. Every line
here has broken a deploy at least once.

**Packages.**

```bash
sudo apt-get install -y python3.12 python3.12-venv python3.12-dev \
    build-essential nginx certbot python3-certbot-nginx
```

`python3.12-venv` is a separate package from the interpreter on Debian and
Ubuntu. Without it `python3.12` exists, `python3.12 -m venv` fails on
`ensurepip`, and the failure only appears at deploy time. `deploy.sh` works
around it by building the environment without pip and fetching pip separately,
and says loudly that it did — but that is a workaround, not a fix.

**A service account.** The units run as `als` and the deploy connects as `als`.

```bash
sudo useradd --create-home --shell /bin/bash als
```

**The checkout**, at `/opt/als-backend`, owned by `als`, on an **HTTPS** remote:

```bash
sudo -u als git clone https://github.com/opa-production/als-backend.git /opt/als-backend
```

Not the `git@github.com:` form. That needs a key registered with GitHub, and the
deploy key travels the other way — GitHub Actions into this server. `deploy.sh`
repairs an SSH remote automatically for a public repo; for a private one, add a
read-only deploy key and keep the SSH URL.

**The virtualenv**, at `/opt/als-backend/.venv`:

```bash
sudo -u als python3.12 -m venv /opt/als-backend/.venv
sudo -u als /opt/als-backend/.venv/bin/pip install /opt/als-backend
```

`deploy.sh` builds one if it is missing, so this is a convenience rather than a
requirement.

**The secrets file**, at `/etc/als-backend/env` — outside the git tree, so a bad
checkout can never expose it. Copy `.env.example` and fill it in; that file
documents every variable and what breaks without it.

```bash
sudo install -d -m 755 /etc/als-backend
sudo install -m 640 -o root -g als /dev/null /etc/als-backend/env
sudo nano /etc/als-backend/env
```

Root-owned, group `als`, `0640`. Not `0600`: `deploy.sh` sources this file
before running migrations, as the deploy account, and a file only root can read
sends alembic to the built-in default connection string — `localhost` — on a box
whose database is somewhere else entirely. Group-readable rather than
world-readable is the difference that matters.

**Never put a `.env` inside `/opt/als-backend`.** pydantic-settings reads it from
the working directory, so a stray copy silently overrides this file for anything
run by hand — and `deploy.sh` does `git reset --hard`, so the two disagree the
moment someone edits the wrong one. One file, one place.

**The systemd units.** Both live in `deploy/` in this repo and are installed
from the checkout, so editing them there and copying is the whole update path:

```bash
sudo install -m 644 /opt/als-backend/deploy/als-backend.service /etc/systemd/system/
sudo install -m 644 /opt/als-backend/deploy/als-worker.service  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now als-backend als-worker
```

`als-worker` is the extraction worker. Without it, uploaded PDFs sit at
`pending` forever, nothing is ever indexed, and the tutor answers every
coursework question with "I could not find this in your material" — which looks
like the tutor being broken rather than a worker being down.

**The sudoers rule.** This is the one CI genuinely cannot do for itself, and its
absence is what makes a deploy stop after installing the code and running the
migrations:

```bash
printf 'als ALL=(root) NOPASSWD: /bin/systemctl restart als-backend\n' \
    | sudo tee /etc/sudoers.d/als-backend
printf 'als ALL=(root) NOPASSWD: /bin/systemctl restart als-worker\n' \
    | sudo tee -a /etc/sudoers.d/als-backend
sudo chmod 440 /etc/sudoers.d/als-backend
sudo visudo -cf /etc/sudoers.d/als-backend
```

Two exact commands, not `NOPASSWD: ALL`. The CI deploy key can reach this
account, so a blanket rule would make that key equivalent to root.

**nginx.** `deploy/nginx.conf` is the site, with `__DOMAIN__` to substitute:

```bash
sed "s/__DOMAIN__/als.ardena.xyz/g" /opt/als-backend/deploy/nginx.conf \
    | sudo tee /etc/nginx/sites-available/als-backend > /dev/null
sudo ln -sf /etc/nginx/sites-available/als-backend /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Then, once DNS resolves: `sudo certbot --nginx -d als.ardena.xyz`. certbot
rewrites the site in place to add TLS and the HTTP→HTTPS redirect. After that,
edit `deploy/nginx.conf` here and re-run the `sed` above rather than hand-editing
the installed file — then certbot again.

**The catch-all.** `deploy/nginx-catchall.conf` answers everything that did not
ask for the domain by name — the bare IP, and any other Host header — with 444,
which closes the connection without a response. Without it those requests reach
FastAPI and fill `journalctl -u als-backend` with 404s for `eval-stdin.php` and
`/containers/json`, which is how a real error gets missed. No substitution; it
names no domain:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo cp /opt/als-backend/deploy/nginx-catchall.conf \
    /etc/nginx/sites-available/als-catchall
sudo ln -sf /etc/nginx/sites-available/als-catchall /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

The `rm` matters: Debian's stock site already claims `listen 80 default_server`,
and two blocks claiming it fails `nginx -t`. Install this *after* certbot has
run — certbot chooses a block by `server_name`, and this one names nothing, but
the 443 block here is a default that only makes sense once the API has its own.
On nginx older than 1.19.4 (`nginx -v`; Ubuntu 22.04 ships 1.18) the
`ssl_reject_handshake` line needs replacing — the file says what with.

To confirm it works, from your laptop:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://<the-ip>/       # empty reply
curl -sS -o /dev/null -w '%{http_code}\n' https://als.ardena.xyz/health
```

The first should fail with "Empty reply from server", which is 444 doing its
job. The second must still return 200 — if it does not, the catch-all is
shadowing the API and the `server_name` in the site is wrong.

**The CI key.** A keypair whose public half is in `/home/als/.ssh/authorized_keys`
and whose private half is the `VPS_SSH_KEY` repository secret. If SSH is already
set up for this box, that key is whatever is already working — nothing here
needs a new one.

**GitHub's host key**, so the deploy's own `git fetch` is neither prompted nor
spoofable:

```bash
sudo -u als ssh-keyscan -t ed25519 github.com >> /home/als/.ssh/known_hosts
```

**Firewall**, if ufw is in use: allow OpenSSH and 'Nginx Full'.

---

### 3. Kora


Payments run on [Kora](https://korahq.com). Two settings, both from the
dashboard, and one thing to get right on their side.

**Environment.** Copy the API keys into the service's environment:

```
KORA_SECRET_KEY=sk_live_...
KORA_PUBLIC_KEY=pk_live_...
PUBLIC_BASE_URL=https://als.ardena.xyz
```

`KORA_WEBHOOK_SECRET` stays blank. Kora signs webhooks with the secret key
itself — there is no separate webhook secret to copy, unlike Paystack.

`PUBLIC_BASE_URL` is not decoration. Every charge is opened with
`notification_url` set to `{PUBLIC_BASE_URL}/api/v1/billing/webhook`, because
the address cannot be derived from an inbound request: behind nginx the app
only ever sees `127.0.0.1:8000`. Setting it per environment is also what stops
a staging deploy from registering itself for production's webhooks.

**Dashboard.** Set the webhook URL to the same address as a fallback, and
enable the `charge.success` event. Nothing else is required — the charge itself
carries the notification URL.

**Check it works** once the keys are in:

```bash
curl -s https://als.ardena.xyz/api/v1/billing/plans | jq
```

Then buy something in test mode and watch for `kora_webhook_applied` in the
logs. If you see `kora_webhook_bad_signature` instead, the signature is being
computed over the wrong material — see below.

#### Two things that differ from Paystack

Both are silent when wrong, which is why they are written down rather than left
in the code for someone to rediscover.

**The amount is the major unit.** `350` means KES 350. Paystack took the minor
unit, so the old integration multiplied by 100. Reintroducing that multiplier
would bill KES 35,000 for a Synapse plan, and nothing in the system would
object — the charge would simply succeed.

**The webhook signature covers only the `data` object**, hashed with SHA-256
and keyed on the secret key. Paystack signed the whole raw body with SHA-512.
Getting this wrong does not fail loudly either: every genuine delivery is
rejected as a forgery, payments stop being credited, and the only symptom is
students saying they paid. `app/services/kora.py` slices the original `data`
bytes out of the body rather than re-serialising the parsed object, because a
round trip through Python turns `350.00` into `350.0` and changes the digest.

## Day-to-day

Deploys happen on push to `master`. Nothing to run by hand.

| | |
|---|---|
| Logs, live | `journalctl -u als-backend -f` |
| Logs, last 100 | `journalctl -u als-backend -n 100 --no-pager` |
| Status | `systemctl status als-backend` |
| Restart | `sudo systemctl restart als-backend` |
| Change a secret | `sudo nano /etc/als-backend/env` then restart |
| Deploy by hand | `bash /opt/als-backend/scripts/deploy.sh <sha>` |
| nginx logs | `/var/log/nginx/als-backend.{access,error}.log` |

**OTP codes go to the journal** while `SMS_API_KEY` is blank, which is how you
test signup before the Celcom account is live:

```bash
journalctl -u als-backend -f
```

**The app store review account.** Google Play and the App Store both ask for a
working login in the review notes, and this product has none to give: sign-in is
Google, or a code texted to a phone the reviewer does not hold. Two variables in
`/etc/als-backend/env` declare one number that takes a fixed code instead:

```bash
REVIEW_PHONE=+254999000001
REVIEW_OTP_CODE=428913
```

Asking for a code on that number sends no SMS and writes nothing to the
database — the code above is the only one it ever accepts — and signing in with
it puts the account on a Synapse plan renewed on every sign-in, so a reviewer
coming back months later still does not meet the paywall. Everything else about
it is an ordinary account.

What goes in the Play Console under *App content → App access → All
functionality is available with the credentials below*:

| | |
|---|---|
| Username | `+254999000001` |
| Password | `428913` |
| Instructions | Choose **Continue with phone**, enter the number above, tap send, then enter the code above. No SMS is sent to this test number; the code is fixed. |

Two rules if it is ever changed. The number must be one no real person can be
issued — `0999` is not a range any Kenyan operator assigns, which is why this
one is safe — or whoever holds it signs in without a code. And both variables
must be set: blank either one and the account does not exist, which shows up as
a reviewer who cannot get in. The service logs `review_account_enabled` with
the number at startup while it is live, so it is never a surprise:

```bash
journalctl -u als-backend | grep review_account
```

**`/docs` is off in production.** `ENVIRONMENT=production` disables Swagger and
`/openapi.json` deliberately. To browse the API against the live server, set
`ENVIRONMENT=staging` temporarily — but note that also disables the startup
guard that refuses a default `JWT_SECRET`, so put it back.

### When a deploy fails

First, ask the server. `scripts/doctor.sh` checks everything section 2 sets up
and prints the fix for whatever is missing:

```bash
ssh als@als.ardena.xyz 'bash -s' < scripts/doctor.sh
```

It is read-only, needs no sudo, and always exits 0. Run it before reading logs —
most deploy failures here have been one missing package, one missing sudoers
line, or a checkout the running service was never using, and this names which.

`scripts/deploy.sh` will not leave a broken service running. If `/health` does
not answer within 30 seconds of the restart it resets the checkout to the
previous commit, reinstalls and restarts, then exits non-zero so the Actions run
goes red. The server keeps serving the last good commit.

It deliberately does **not** roll back the database. An automatic
`alembic downgrade` is far more dangerous than a failed deploy — migrations here
are additive, so the previous code runs fine against the newer schema. If a
migration is what broke, un-pick it by hand.

### Schema changes

After changing a model:

```bash
alembic revision --autogenerate -m "what changed"
```

CI runs `scripts/check_migrations.py`, which compiles the migrations to DDL and
diffs them against `Base.metadata`. A model change with no migration fails the
build rather than silently leaving production a column behind. The check needs
no database.

Migrations run *before* the restart, while the old code is still serving. That
is only safe for additive changes. To remove a column: ship it nullable, backfill,
drop it in a later release.

---

## Scaling past one box

`WEB_CONCURRENCY` in `/etc/als-backend/env` sets the worker count (2 per core is
a reasonable start for an IO-bound service). Each worker holds its own pool of
`DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW` connections — keep the total under
the Supabase plan's connection ceiling.

Before running more than one *machine*, move `alembic upgrade head` out of
`scripts/deploy.sh` into a step that runs once per release. With several servers,
each one racing the same DDL, the losers crash-loop.

---

## The Render blueprint

`render.yaml` is still in the repo. It stands up a free-tier Render service and
database, and was the throwaway host used before the VPS. It is not part of this
deployment path — delete it once you are confident on Contabo.
