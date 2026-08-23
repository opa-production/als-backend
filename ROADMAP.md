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

Each of these depends on something outside the process: a bucket, Paystack, a
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
| `POST /billing/verify` | Paystack secret key. |
| `POST /billing/webhook` | Paystack webhook secret. **This is what makes a subscription real** — the app currently writes `verified: false` on a student's word. |
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

## The rule that keeps the order honest

An endpoint moves up a tier the moment it needs a credential nobody has set
yet. That is not pedantry: a route written against an imaginary provider gets
its error handling wrong in ways nobody notices until the provider is real.
