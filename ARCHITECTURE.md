# ALS backend — architecture

Ardena Learning System. FastAPI, async end to end, Postgres via SQLAlchemy 2.0,
Supabase Storage for files, Alembic for schema.

No endpoints exist yet. This document and the scaffold under `app/` are the
shape everything gets built into.

---

## 1. What the client already assumes

The mobile app is local-first and already works with no server. That is not a
gap to close — it is the contract this backend has to fit into:

- **Ids are minted on the device** (`crypto.randomUUID`). Every write is an
  upsert on a client-chosen id, which is what makes sync retries safe. The
  server must never mint an id for a row the client created, or a flaky
  connection turns one note into four.
- **Everything is `{ data, error }`, never an exception.** The client's
  `src/api/client.js` already normalises this. Errors carry a message meant to
  be shown to a student.
- **The device is a full replica.** The server is the durable copy and the
  arbiter for anything a device cannot decide — payment, group seats, PDF text
  — not a prerequisite for the app opening.
- **`src/api/endpoints.js` in the app names every call.** That file is the
  interface list; this backend implements it.

---

## 2. Postgres or Supabase Storage

The rule: **Postgres holds what you query. Storage holds what you stream.**

Large binaries in Postgres are a slow disaster — they bloat the WAL, make every
backup an hour longer, and hold a pooled connection open for the length of a
download. Equally, metadata in object storage cannot be filtered, joined or
counted, so it does not belong there either.

### Goes to Supabase Storage

| Bucket | Contents | Access |
| --- | --- | --- |
| `materials` | Lecture PDFs, slide decks, past papers | Private, signed URL |
| `scans` | Photos of handwritten notes, OCR sources | Private, signed URL |
| `avatars` | Profile pictures | Private, signed URL |
| `exports` | Generated revision packs, PDF summaries | Private, short TTL |

Path convention, which is also the authorisation boundary:

```
{bucket}/{user_id}/{unit_id}/{material_id}.{ext}
```

Every bucket is **private**. The API issues short-lived signed URLs; nothing is
served from a public bucket, because a public bucket means a leaked path is a
permanent leak. Uploads use signed upload URLs too, so file bytes never pass
through the API process — that is what keeps a 50 MB PDF from occupying a
worker for the length of a student's upload.

### Goes to Postgres

Everything else, and specifically:

- **Extracted text from PDFs and OCR.** This is the single most important line
  in this document. The file goes to Storage; the *text pulled out of it* goes
  into `material_chunks` in Postgres. The tutor searches text, so the text has
  to be somewhere searchable. It is also what finally makes
  `totalPdfPagesPool`, `maxSingleFilePages` and the OCR limits enforceable —
  none of them can be checked on the device, because nothing there can read a
  PDF.
- Units, class sessions, events, chats, messages.
- Material *metadata*: title, kind, page count, byte size, storage path.
- Subscriptions, group seats, usage counters.

### Goes to neither

Card numbers, M-Pesa PINs, anything Paystack holds. The webhook gives us a
reference and a status; that is all we store.

---

## 3. Why async, and where it actually matters

`async` is not free — it is a correctness burden on every line that touches IO.
It is worth it here because the load profile is almost entirely waiting:

- Supabase Storage calls (network)
- Paystack verification (network)
- The LLM call behind `/tutor/ask` (network, and *seconds* not milliseconds)
- Postgres round trips

A sync worker blocked for three seconds on a model response serves one student.
An async worker serves hundreds. Rules that follow from that:

- **`asyncpg` + SQLAlchemy 2.0 async.** No sync `psycopg2` path anywhere.
- **`httpx.AsyncClient`, one shared instance** on app state, created in the
  lifespan. A new client per request leaks connections and re-does TLS.
- **Never call blocking code in a handler.** PDF parsing, OCR and image
  resizing are CPU-bound: they go to a worker (`app/workers/`), not into the
  request path. A handler that blocks the event loop stalls *every* other
  request on that process, which is the failure mode that looks like "the whole
  API went down" when one endpoint got slow.

---

## 4. Standing up to load

- **Stateless processes.** No in-memory sessions, no local file writes. Scale
  is `--workers N` and then more containers.
- **Connection pooling, twice.** SQLAlchemy pools per process; a pooler
  (PgBouncer, or Supabase's) sits in front of Postgres, because `workers ×
  pool_size × containers` overruns `max_connections` quickly. In transaction
  pooling mode, prepared statements must be disabled — see
  `app/db/session.py`.
- **Idempotency for free.** Client-minted ids mean `POST /sync` can be retried
  without an `Idempotency-Key` header. Payment confirmation gets one anyway,
  since a double-charge is not recoverable by shrugging.
- **Rate limits per user, not per IP.** A campus shares one NAT address; per-IP
  limits would throttle a whole lecture hall together.
- **The expensive endpoints are known in advance.** `/tutor/ask` and
  `/materials/upload` are the only two that are slow, and both are metered by
  the plan already defined in the app's `src/theme/plans.js`. That file is the
  source of truth for limits and is mirrored in `app/services/quota.py` — with
  the server as the *authority*, since the client copy is unenforceable by
  definition.
- **Timeouts on everything outbound.** A hung upstream must fail in seconds,
  not hold a worker until the pod is killed.

---

## 5. Migrations run themselves

`alembic upgrade head` runs as a **release step**, before new containers take
traffic — not on app start. Running migrations in the app's lifespan means N
containers racing the same DDL on every deploy.

`alembic/env.py` is async and imports `app.models` so `--autogenerate` sees
every table. `scripts/release.sh` is the command a platform runs.

Two rules that keep deploys boring:

1. **Migrations are backwards-compatible for one release.** Add a nullable
   column, deploy, backfill, then make it `NOT NULL` in the *next* release.
   Old and new code run side by side during a rolling deploy.
2. **No destructive DDL in the same release as the code that stops using a
   column.** Drop it a release later, once nothing can roll back onto it.

---

## 6. Layout

```
app/
  main.py            FastAPI app factory, lifespan, Swagger config
  core/
    config.py        Settings, from the environment
    logging.py       Structured JSON logs, request ids
    errors.py        Exception handlers -> the client's { data, error }
    security.py      JWT mint/verify
  db/
    base.py          DeclarativeBase, naming convention
    session.py       Async engine, sessionmaker, request-scoped session
  models/            SQLAlchemy tables — the durable shape
  schemas/           Pydantic request/response models
  api/
    deps.py          Auth, pagination, db session dependencies
    v1/router.py     Aggregates route modules
    v1/routes/       One module per resource (empty for now)
  services/
    storage.py       Supabase Storage adapter, signed URLs
    quota.py         Plan limits, enforced server-side
  workers/           Background jobs: PDF text, OCR, embeddings
```

Layering rule: `api` may import `services` and `schemas`; `services` may import
`models` and `db`; nothing imports `api`. Handlers stay thin — they parse,
authorise, delegate, and shape a response.

---

## 7. What is deliberately not here yet

- No endpoints. The next task.
- No LLM adapter. `/tutor/ask` receives passages the device already ranked, so
  the server only has to generate — retrieval stays on the phone.
- No embeddings. `material_chunks` is laid out so a `pgvector` column can be
  added when retrieval outgrows the device.
