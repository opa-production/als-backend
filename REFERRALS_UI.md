# Referrals — app implementation

For whoever builds these screens. The backend is done, tested and behind
`GET /api/v1/me/referrals` plus one new field on sign-in; this is the contract,
the copy, and the decisions that are already made.

**The whole feature is three surfaces:** a card on the profile screen, a share
sheet, and one optional field in onboarding. There is no leaderboard, no
history list, no "invite your contacts" permission prompt.

---

## 1. How the programme works

Say it back to yourself in one sentence, because every screen has to be
truthful about it:

> **Anyone can share their code. Nothing is earned until the friend who used it
> pays.**

| Who | What they get |
| --- | --- |
| The friend, at their first purchase | **+7 days** on whatever plan they bought |
| A referrer **on a paid plan** | **+14 days** on their plan (**+30** if the friend bought a Season) |
| A referrer **on Free** | The same days, **banked** — they start the day the referrer subscribes |

A referred student who paid is an ordinary account afterwards. They earn by
referring somebody, exactly like everyone else. **Nothing about being referred
keeps paying them**, and the app must not imply otherwise.

Rewards sit on a **seven-day hold** before they are credited. The app does not
need to model that — the API folds pending days into `days_banked` — but it is
why days do not appear the instant a friend pays.

---

## 2. The API

### `GET /api/v1/me/referrals`

```json
{
  "code": "K7M2QX",
  "joined": 5,
  "paid": 2,
  "days_earned": 28,
  "days_banked": 14,
  "banked_pending_subscription": true,
  "friend_days": 7
}
```

| Field | Meaning |
| --- | --- |
| `code` | Six characters, no I/O/0/1. **Minted by the first call to this endpoint** and stable forever after. |
| `joined` | Signed up with the code. Has *not* necessarily paid. |
| `paid` | …and paid. This is the number that earned something. |
| `days_earned` | Already added to their plan. Cumulative, lifetime. |
| `days_banked` | Earned but not on a plan yet — either inside the hold or waiting for the student to subscribe. |
| `banked_pending_subscription` | `true` when those banked days are waiting on a **subscription**, not on the hold. This is what decides the card's call to action. |
| `friend_days` | What a friend gets. **Use this in the share message** — never hardcode 7, or the message and the server drift apart. |

Requires a token. Nothing in the payload identifies anyone: no phone numbers,
no names, no list of who joined. Don't ask for one; it isn't coming.

### `POST /api/v1/auth/otp/verify` — one new optional field

```json
{ "phone": "+254712345678", "code": "123456", "device_id": "…",
  "referral_code": "K7M2QX" }
```

`POST /api/v1/auth/google` takes the same `referral_code` field.

**It is only read when that request creates the account.** Sending it on a
later sign-in does nothing — attribution is written once and never again,
because a code that can be added later is a code somebody adds after they have
already paid.

A wrong or unknown code **never fails the sign-in**. It is silently ignored and
the account is created normally. Do not validate the code client-side, do not
block the button on it, and do not show an error if it turns out to be
unknown — a student mistyping their friend's code must still get an account.

---

## 3. The profile card

One card in the profile screen, above the feature-request row.

**Free student, nothing earned yet:**

> ### Get free days
> Share your code. When a friend subscribes, you both get free days — they get
> a week, you get two.
>
> `K7M2QX`  [ Share ]

**Free student with banked days** — `banked_pending_subscription: true`:

> ### You've earned 14 free days
> They start the day you subscribe.
>
> `K7M2QX`  [ Share ]   [ See plans ]

That second button is the point of the whole banked mechanic. Route it to the
plans screen.

**Paid student:**

> ### Get free days
> 2 friends have subscribed · 28 days earned
>
> `K7M2QX`  [ Share ]

Rules for the card:

- **Tapping the code copies it**, with a toast: *"Code copied"*. People will
  try this before they find the Share button.
- Show `joined` only when it is bigger than `paid` and `paid` is at least 1 —
  *"5 signed up, 2 subscribed"* is honest and motivating. Never show `joined`
  alone: *"5 friends joined"* next to zero days reads as the app owing them
  something.
- With `paid == 0`, show no counts at all. An empty state full of zeros is a
  screen telling someone they have failed.

## 4. The share sheet

Native share sheet, one message. Build it from the API fields, not from
constants in the app:

> Ardena helps me revise from my own lecture notes and past papers.
> Use my code **K7M2QX** when you sign up and you get 7 days free.
> [link]

If you have a deep link, put the code in it (`ardena.app/join/K7M2QX`) so the
friend never has to type it, and prefill the onboarding field from the link.
The typed code has to keep working regardless — most of these will be pasted
into a WhatsApp group and read off a screen.

Do **not** write "I get free days too" into the message. It is true, it is
fine, and it is not what makes somebody tap. The offer is the friend's week.

## 5. Onboarding

One optional field on the sign-up screen, below the phone number:

> Referral code (optional)
> `______`

- Uppercase as they type. Strip spaces.
- Max 6 characters, alphanumeric.
- **Never block Continue on it.** Not while empty, not while wrong.
- Prefilled and collapsed to a confirmation line if they arrived by deep link:
  *"Joining with K7M2QX"*.

**Do not show this field to an existing account signing back in.** The server
ignores it there, and offering it invites a support ticket that begins "I typed
my friend's code and nothing happened."

## 6. Where days actually show up

Referral days extend the subscription's expiry. They surface on the existing
billing screen as a later renewal date and a bigger `days_remaining` — there is
no separate "bonus days" balance to render, and no new billing field.

The one thing worth adding: after a friend's payment vests, the profile card's
`days_earned` goes up. If you want a moment, show a one-time toast the next
time the number increases. Don't build a notification for it in v1.

## 7. Things that are deliberately absent

- **No list of who joined.** The payload has counts, not people. A screen
  naming who did and did not subscribe is a screen that makes students chase
  their friends.
- **No leaderboard, no tiers, no ambassador badge.** If campus ambassadors
  happen, they will be approved by hand in the console.
- **No cash, anywhere.** Not a balance, not a withdrawal, not "worth KES 70".
  The reward is time on a plan. Framing it in shillings invites the question of
  how to cash it out, and there is no answer to that question.
- **No expiry countdown on banked days.** They do expire — 90 days — and the
  bank caps at 60 days, but a countdown on a gift reads as a threat. If a
  student writes in, support can see the dates.
- **No "remind your friend to pay" nudge.** That is the app asking a student to
  pressure their friends on our behalf.

## 8. Checklist

- [ ] Profile card, three states (nothing yet / banked / paid)
- [ ] Tap-to-copy on the code, with toast
- [ ] Share sheet built from `friend_days`, not a constant
- [ ] "See plans" button when `banked_pending_subscription` is true
- [ ] Counts hidden entirely when `paid == 0`
- [ ] `referral_code` sent on OTP verify **and** Google sign-in
- [ ] Field never blocks Continue and never shows a validation error
- [ ] Field hidden for an account that already exists
- [ ] Deep link prefills the field, typed codes still work
- [ ] Nothing anywhere renders the reward as money
