# Feature requests — app implementation

For whoever builds this screen in the mobile app. The backend is done and
deployed behind `POST /api/v1/me/feature-requests`; this is the contract, the
copy, and the decisions that are already made so they do not get re-litigated
in the UI.

**The whole feature is one text box, a Send button, and a modal.** If the
design has grown a title field, a category picker, a vote count or a "your
requests" list, it has grown past what the server supports and past what the
feature is for. Every field on this form is a reason not to fill it in, and
what is being collected is the sentence somebody types the moment the app
cannot do the thing they opened it for.

---

## 1. The one call

```
POST /api/v1/me/feature-requests
Authorization: Bearer <access token>
Content-Type: application/json
```

```json
{
  "body": "Please let me download my notes as audio so I can revise in the matatu.",
  "app_version": "1.4.2",
  "platform": "android"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `body` | yes | 10–2000 characters after trimming. The whole submission. |
| `app_version` | no | Whatever the app reports for itself. Send it. |
| `platform` | no | `android` or `ios`. Max 16 chars. |

`app_version` and `platform` are not validated and are not trusted for
anything. They exist so "the timetable is empty" can be read against the build
it came from, instead of guessed at. Sending them costs nothing and saves a
support round trip, so send them.

**201 Created:**

```json
{ "message": "Thanks — your idea is with the team.", "id": "3f2c…" }
```

`message` is the modal's body text. It comes from the server so the wording can
change without a release. `id` is for support — it is deliberately *not*
something the app stores or displays.

There is **no `GET`**. Do not build a "your requests" screen; the route does not
exist and will 405. See §5.

## 2. Errors

Every failure comes back in the shape `src/api/client.js` already normalises —
`{ "message": "…" }` — so `error.message` is always safe to show verbatim.

| Status | When | What to show |
| --- | --- | --- |
| `400` | Under 10 characters after trimming | The server's message: *"Tell us a little more about what you would like the app to do."* |
| `422` | Over 2000 characters | Should never happen — the input is capped client-side. Show the message. |
| `429` | Sixth submission in 24 hours | The server's message: *"Thanks — that is a few ideas already today. Send more tomorrow."* |
| `401` | Token expired or missing | The normal refresh-then-retry path. If refresh fails, route to sign-in. |
| network | No connection | *"You are offline. Try again when you have a connection."* Keep the text in the box. |

Show all of these in the **same modal** as success, with a different icon and
the message swapped in. One component, two states — a separate error toast is
where the text someone typed gets lost.

## 3. The screen

**Entry point:** Profile → a row reading **"Suggest a feature"**, under the
account rows and above sign-out. A row, not a floating button: this is
something a student goes looking for once, not a thing that should follow them
around the app.

**The sheet:**

- Title: **What should we build?**
- Sub-line: *Tell us what would make revision easier. We read every one.*
  Only write that second sentence if it is true. It currently is.
- A multiline text input, ~5 lines tall, autofocused, `maxLength={2000}`.
  Placeholder: *"I wish the app could…"*
- A character counter that appears only past ~1800, so it warns rather than
  nags.
- A **Send** button, disabled until 10 non-whitespace characters are in the
  box. Disabled-until-valid is what makes the 400 unreachable in practice.
- Dismissing the sheet with text in it should keep the draft in component state
  for the session. Do not persist it to storage — an unsent idea resurfacing a
  week later is confusing, and it is not the student's content in the way a
  note is.

**On send:** disable the button and show a spinner *on the button*, not a
full-screen blocker. The call is one insert; it returns in well under a second
on a normal connection.

**On success:** dismiss the sheet, then show the modal with the server's
`message` and a single **Done** button. Clear the input. Do not navigate
anywhere.

**Double-submit:** the disabled-while-sending button is the guard. Do not add a
client-side dedupe of identical text — a student who sends the same thing twice
has told us something, and the server's daily cap makes the worst case five
rows.

## 4. Offline

Do **not** queue this into the sync engine. Everything in `/sync` is the
student's own content, replicated to a device that must work with no server; a
feature request is a message to us and has no reason to exist on the phone
after it is sent. If the send fails, say so and leave the text in the box for
them to retry — a queued submission that fires three days later, out of the
context that prompted it, is worse than one that was never sent.

## 5. Things that are deliberately absent

Worth knowing so nobody adds them back as an "improvement":

- **No list of your own requests.** Such a screen has exactly one honest state —
  a paragraph with no answer beside it — and that reads as being ignored rather
  than as being heard.
- **No public board, no votes, no other students' ideas.** That is a forum, and
  a forum is a moderation surface, a reporting flow, and a place for a phone
  number to end up.
- **No status, no reply, no "we're working on it" badge.** A triage board
  nobody keeps current makes a stale list look like a decision. If a request is
  worth answering, it is answered by building the thing.
- **No category picker.** Categories can be derived from the paragraphs later;
  the paragraph cannot be recovered from a dropdown nobody used.

## 6. Where it goes

The console reads these at `GET /api/v1/admin/feedback/feature-requests` —
newest first, searchable by text, filterable by student and by date, with the
requester's name, institution and current plan beside each one. Phone numbers
are not in that response by design.

## 7. Checklist

- [ ] Profile row → sheet opens
- [ ] Send disabled under 10 characters, input capped at 2000
- [ ] `app_version` and `platform` sent
- [ ] 201 → sheet dismissed, modal shows the server's `message`, input cleared
- [ ] 429 → same modal, server's message, text kept in the box
- [ ] Offline → same modal, offline copy, text kept in the box
- [ ] 401 → refresh and retry once, then sign-in
- [ ] Button disabled while the request is in flight
- [ ] No list screen anywhere
