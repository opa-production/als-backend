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

Card numbers, M-Pesa PINs, anything Kora holds. The webhook gives us a
reference and a status; that is all we store.

---

## 3. Why async, and where it actually matters

`async` is not free — it is a correctness burden on every line that touches IO.
It is worth it here because the load profile is almost entirely waiting:

- Supabase Storage calls (network)
- Kora verification (network)
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
    v1/routes/       One module per resource
    v1/routes/admin/ The console — its own auth, its own token type (§7)
  services/
    storage.py       Supabase Storage adapter, signed URLs
    quota.py         Plan limits, enforced server-side
    analytics.py     Every number the console shows, computed live
    audit.py         Appends an admin action, in the caller's transaction
  workers/           Background jobs: PDF text, OCR, embeddings
```

Layering rule: `api` may import `services` and `schemas`; `services` may import
`models` and `db`; nothing imports `api`. Handlers stay thin — they parse,
authorise, delegate, and shape a response.

---

## 7. The admin console

`/api/v1/admin/*`. Same process, same database, deliberately separate identity.

### Why a second table and not a flag

`admin_users` is its own table rather than `users.is_admin`, for three reasons
that each stand alone:

- **The credential is different.** A student signs in with an SMS code to a
  Kenyan number; an admin signs in from a laptop with a password. A password
  column on `users` is a hashable secret on ten thousand rows that will never
  have one.
- **The token is different.** Admin tokens carry `typ: admin` and are refused
  by every student endpoint; student tokens are refused by every admin one.
  Both are signed with the same secret, so `typ` is the whole separation — see
  `decode_admin_token` in `app/core/security.py`. With one table and a boolean,
  a stolen student token plus a flipped flag is total access.
- **Blast radius.** Nothing under `/admin` is scoped to one account.

Passwords use `hashlib.scrypt` — a memory-hard KDF from the standard library,
rather than adding passlib and a C extension for a table that holds single
digits of rows. Parameters are stored with each digest so they can be raised
later without invalidating existing passwords.

### Roles

Three, ranked and strictly nested: `support` < `admin` < `owner`. A rank
comparison rather than a permission matrix, because the roles genuinely nest
and a matrix develops gaps.

| Role | Can |
| --- | --- |
| `support` | Read everything. Release a device lock — the one privileged action that grants nothing. |
| `admin` | Grant and revoke plans, reset usage counters, reconcile payments, delete and restore accounts. |
| `owner` | Create, change and remove other admins. |

The role is read from the row on every request, never from the token. A
demotion takes effect immediately rather than at the next sign-in.

### The audit log

`admin_audit_log` is appended in the same transaction as the change it
describes, so it cannot record an action that was rolled back. Reads are not
logged — logging them buries the twenty entries that matter under twenty
thousand that do not. The one exception is a successful sign-in, because "who
was in, from where" is the first question anyone asks afterwards.

Nothing in the API deletes or edits an entry, and `admin_id` is `ON DELETE SET
NULL` with the email denormalised alongside it, so removing an administrator
does not anonymise what they did.

### Where the numbers come from

`app/services/analytics.py`, computed live from the same tables the product
writes. No rollup table and no nightly job: an aggregate is faster and is also
a second copy of the truth that goes wrong quietly, and quietly-wrong revenue
is worth a few hundred milliseconds to avoid.

Two definitions are used consistently:

- **Active** — `expires_at` in the future *and* `verified`. The same test
  `app/services/quota.py` applies before letting anyone spend a quota, so the
  console cannot claim someone is paying while the API refuses them.
- **Paying** — active *and* on a tier that costs money.

The Friends plan is the one place arithmetic goes wrong by default: one payment
of KES 1,250 creates up to five subscriptions on `tier = friends`, so MRR is
counted **per group** while seats are counted per person. Both numbers are
correct and they answer different questions.

### The endpoint that matters most

`POST /admin/payments/{reference}/reconcile`. Webhooks are delivered over the
internet: a student pays, Kora fires, the request is dropped or a container
is mid-deploy, and the money is real while the subscription is not. Nothing
notices — the student is charged and locked out, and the first signal is a
complaint. This re-reads Kora's own record and re-runs the same activation
path the webhook would have, idempotently.

`GET /admin/overview` surfaces the same problem as an `attention` item before
anyone opens a ticket.

### Bootstrapping

There is no sign-up and no seeded account. The first administrator is created
at a shell:

```
python scripts/create_admin.py --email you@ardena.co.ke --role owner
```

A default password baked into a migration is a back door that ships to every
environment and is remembered in none of them.

Login is rate limited in nginx rather than in the application (`deploy/nginx.conf`),
because the limit has to reject a request before it costs a worker — the KDF is
deliberately slow, which makes an unthrottled login form a way to occupy the
whole pool.

---

## 8. What is deliberately not here yet

- No endpoints. The next task.
- No LLM adapter. `/tutor/ask` receives passages the device already ranked, so
  the server only has to generate — retrieval stays on the phone.
- No embeddings. `material_chunks` is laid out so a `pgvector` column can be
  added when retrieval outgrows the device.
