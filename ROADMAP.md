# API roadmap

Three tiers, ordered by how much has to be true before an endpoint can work.
Not by how much code it takes — by how many unknowns it carries.

---

## Easy — self-contained

Nothing outside this service has to exist first. One request in, one row
changed, one response out. Testable entirely through Swagger.

| Endpoint | Notes |
| --- | --- |
| `POST /auth/otp` | Send a code. Throttled — an unthrottled one is a bill. |
| `POST /auth/otp/verify` | Code → tokens. Creates the account on first use. |
| `POST /auth/google` | Google ID token → tokens. |
| `POST /auth/refresh` | Rotate. |
| `POST /auth/logout` | Revoke this device. |
| `GET /me` | Profile. |
| `PATCH /me` | Update profile. |
| `DELETE /me` | Delete the account and everything under it. |
| `GET /health`, `GET /ready` | Liveness, and readiness that does touch the DB. |

**Status: implemented.**

---

## Intermediate — needs another system to be real

Each of these depends on something outside the process: a bucket, Kora, a
push service. The logic is not hard; the integration is where the surprises
live.

| Endpoint | Blocked on |
| --- | --- |
| `POST /sync`, `GET /sync?since=` | Nothing technically — but it is the one place where conflict resolution has to be decided rather than discovered. Last-write-wins on `updated_at`, with client ids making it idempotent. |
| `POST /materials/upload-url` | Supabase keys. Signs an upload; bytes never touch this API. |
| `POST /materials/{id}/complete` | Confirms the upload landed, then queues extraction. |
| `GET /materials/{id}/download-url` | Short-lived signed URL. |
| Units / events / classes CRUD | Mostly covered by `/sync`; discrete routes only where the app needs one row. |
| `GET /billing/subscription` | — |
| `POST /billing/checkout` | Kora secret key. Issues the payment link, so the charge carries a user id. |
| `POST /billing/verify` | Kora secret key. |
| `POST /billing/webhook` | Kora webhook secret. **This is what makes a subscription real** — the app currently writes `verified: false` on a student's word. |
| Friends group: create / invite / join / members | Seat accounting. |
| Usage counters and enforcement | Mirrors `src/theme/plans.js`, with the server as the authority. |
| Notification scheduling | Expo push tokens. |

---

## Complex — the RAG system

Deliberately last. Everything here is a pipeline with failure modes that are
invisible until real documents hit it: a scanned PDF with no text layer, a
200-page past paper, a photo of handwriting at an angle.

| Piece | Why it is hard |
| --- | --- |
| PDF text extraction | Page counts and per-page text. CPU-bound, so it belongs in a worker, not a handler. This is what makes `maxSingleFilePages` and `totalPdfPagesPool` enforceable at last. |
| OCR for scans | Slow, wrong often enough to need a confidence signal, and metered per page by the plan. |
| Chunking | Chunk boundaries decide citation quality. Too big and the quote is a page; too small and it loses the sentence. |
| Embeddings + pgvector | Only once device-side retrieval stops being good enough. `material_chunks` is laid out for the column already. |
| `POST /tutor/ask` | Generation over passages the device ranked. Seconds per call, costs money per call, and needs a token budget and a hard timeout. |
| `POST /tutor/quiz` | Same machinery, different prompt. |

The device already does retrieval (`src/lib/tutor.js`), and it will keep doing
it. The server's job in this tier is generation and the things a phone cannot
do — reading a PDF, running OCR.

---

## The back office

Not on the three tiers above, because it depends on nothing outside this
service and blocks nothing inside it. It is `/api/v1/admin/*` — see
ARCHITECTURE.md §7 for why it has its own identity table and its own token
type.

| Group | Endpoints |
| --- | --- |
| Auth | `POST /admin/auth/login`, `/refresh`, `/logout`, `GET /admin/auth/me` |
| Overview | `GET /admin/overview`, `/overview/timeseries`, `/overview/institutions` |
| Users | `GET /admin/users`, `GET|PATCH|DELETE /admin/users/{id}`, `POST /{id}/restore`, `POST|DELETE /{id}/subscription`, `POST /{id}/device-reset`, `GET /{id}/usage`, `POST /{id}/usage/reset`, `GET /{id}/groups` |
| Subscriptions | `GET /admin/subscriptions`, `/subscriptions/stats` |
| Revenue | `GET /admin/revenue/summary`, `/by-plan`, `/timeseries`, `/top-customers` |
| Payments | `GET /admin/payments`, `/payments/{id}`, `POST /payments/{reference}/reconcile` |
| Groups | `GET /admin/groups`, `/groups/{id}` |
| Content | `GET /admin/content/stats`, `/content/materials` |
| Ops | `GET /admin/ops/health`, `/ops/plans` |
| Audit | `GET /admin/audit`, `/audit/actions` |
| Admins | `GET|POST /admin/admins`, `PATCH|DELETE /admin/admins/{id}`, `GET /{id}/sessions` |

**Status: implemented.** Bootstrap the first account with
`python scripts/create_admin.py --email you@ardena.co.ke --role owner`.

The one piece here that is not a read: `POST /admin/payments/{reference}/reconcile`
re-reads Kora and applies the answer, which is how a payment whose webhook
never arrived gets credited without anyone editing a row by hand.

---

## The rule that keeps the order honest

An endpoint moves up a tier the moment it needs a credential nobody has set
yet. That is not pedantry: a route written against an imaginary provider gets
its error handling wrong in ways nobody notices until the provider is real.
