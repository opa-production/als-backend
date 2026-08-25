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

SSH in as root and run:

```bash
git clone https://github.com/Deon62/als-backend.git /tmp/als
sudo bash /tmp/als/scripts/provision.sh \
    --domain api.ardena.co.ke \
    --repo git@github.com:Deon62/als-backend.git
```

This installs Python 3.12, nginx, certbot and ufw; creates the `als` user;
clones the repo to `/opt/als-backend`; builds the virtualenv; installs the
systemd unit and the nginx site; opens the firewall; and generates an SSH key
for CI. It is safe to re-run — re-run it after editing anything in `deploy/`.

It then prints the three remaining steps, which need your input:

**Fill in the secrets.** `sudo nano /etc/als-backend/env` — every `CHANGEME`
must be replaced. Then `sudo systemctl start als-backend`.

**Point DNS at the server** (an `A` record for your domain), wait for it to
resolve, then `sudo certbot --nginx -d api.ardena.co.ke`. certbot rewrites the
nginx site in place to add TLS and the HTTP→HTTPS redirect, and installs a
renewal timer.

**Add the GitHub secrets** it prints: `SSH_HOST`, `SSH_USER`, `SSH_KEY`. If the
repo is private, also add the printed public key as a read-only *deploy key* so
the server can `git fetch`.

Optional but worth doing: set `SSH_HOST_KEY` too (`ssh-keyscan -t ed25519 <ip>`).
Without it, every CI run trusts whatever host answers on that address.

---

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

**`/docs` is off in production.** `ENVIRONMENT=production` disables Swagger and
`/openapi.json` deliberately. To browse the API against the live server, set
`ENVIRONMENT=staging` temporarily — but note that also disables the startup
guard that refuses a default `JWT_SECRET`, so put it back.

### When a deploy fails

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
