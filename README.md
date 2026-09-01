# ALS backend

FastAPI, async end to end, Postgres via SQLAlchemy 2.0, Supabase Storage for
files, Alembic for schema.

Read [ARCHITECTURE.md](./ARCHITECTURE.md) first — it explains the decisions
this scaffold encodes, including what goes to Supabase and what stays in
Postgres.

Then, as they come up:

- [PLAN_LIMITS.md](./PLAN_LIMITS.md) — what each tier limits, and why it limits
  that and not something else.
- [APP_PAYMENTS.md](./APP_PAYMENTS.md) — for the app repo: taking a payment.
  M-Pesa by phone number, cards by redirect, and the one rule about the shared
  Paystack account.
- [APP_EXTRACTION_UX.md](./APP_EXTRACTION_UX.md) — for the app repo: the whole
  contract for telling a student what is happening to their document — status,
  scans, and the notification when one finishes.
- [APP_UPDATES.md](./APP_UPDATES.md) — getting a new build onto a phone: OTA for
  JavaScript, the store and the update modal for everything else.
- [DEPLOYMENT.md](./DEPLOYMENT.md) — the VPS, systemd and nginx.

**No endpoints exist yet.** This is the setup only.

---

## Running it

```bash
cp .env.example .env          # then fill in JWT_SECRET and the Supabase keys
docker compose up --build
```

- API — <http://localhost:8000>
- **Swagger — <http://localhost:8000/docs>** (the test surface for now)
- Health — <http://localhost:8000/health>

Without Docker:

```bash
python -m venv .venv && . .venv/Scripts/activate    # Windows
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

---

## Migrations

```bash
alembic revision --autogenerate -m "add materials"   # write one
alembic upgrade head                                 # apply
alembic downgrade -1                                 # undo the last
```

Autogenerate only sees tables imported in `app/models/__init__.py`. A model
missing from that file gets a migration that **drops** its table — always read
the generated migration before committing it.

Deploys run `scripts/release.sh` as a release step, before new containers take
traffic. Migrating from the app's lifespan would have every container in a
rolling deploy race the same DDL.

Two rules that keep deploys boring:

1. **One release of backwards compatibility.** Add a nullable column, deploy,
   backfill, tighten to `NOT NULL` next release — old and new code run side by
   side while containers cycle.
2. **No destructive DDL in the same release** as the code that stops using the
   column. Drop it a release later, once nothing can roll back onto it.

---

## Layout

```
app/
  main.py            app factory, lifespan, Swagger
  core/              config, logging, errors, security
  db/                engine, session, declarative base
  models/            SQLAlchemy tables — the durable shape
  schemas/           Pydantic request/response models
  api/v1/routes/     one module per resource (empty)
  services/          storage, quota — the logic handlers delegate to
  workers/           PDF text, OCR, embeddings (CPU-bound, off the request path)
alembic/             async migration environment
scripts/release.sh   what a deploy runs before cutting traffic
```

`api` may import `services`; `services` may import `models` and `db`; nothing
imports `api`. Handlers parse, authorise, delegate and shape a response —
business logic lives in `services`.

---

## Non-obvious things worth knowing

- **`DATABASE_URL` must be `postgresql+asyncpg://`.** A plain `postgresql://`
  loads the sync driver silently and every query then blocks the event loop.
  `config.py` rejects it at boot rather than letting you find out under load.
- **Set `DATABASE_USE_PGBOUNCER=true` behind a transaction-mode pooler**,
  including Supabase's. Otherwise asyncpg's prepared-statement cache breaks in
  a way that only appears under concurrency.
- **Ids come from the client.** The mobile app mints UUIDs before a row has
  ever reached the server, which is what makes every write an idempotent
  upsert. Never generate an id for a row the client created.
- **File bytes never pass through this API.** Uploads and downloads use signed
  URLs straight to Supabase. Proxying a 50 MB PDF would tie up a worker for the
  length of a student's upload.
- **PDF and OCR work belongs in `workers/`, not a handler.** It is CPU-bound,
  and blocking the event loop stalls every other request on the process — the
  failure that looks like "the whole API is down" when one endpoint got slow.
- **Errors return `{"message": "..."}`.** The app reads that field and shows
  it, so only `AppError` subclasses carry text meant for a student; everything
  else becomes a generic 500 with the detail logged.

---

## Environment

See `.env.example`. `JWT_SECRET` and `SUPABASE_SERVICE_KEY` have no safe
defaults — `settings.assert_production_ready()` refuses to boot a production
process that still has the placeholders.
