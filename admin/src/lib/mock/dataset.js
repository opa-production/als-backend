/*
 * A fake but coherent database.
 *
 * The point of this file is not "some rows to render". It is that every number
 * the console shows has to agree with every other number, or the design is
 * being judged against data that could never exist. If the dashboard says 214
 * paying customers and the subscriptions table filters down to 189, you cannot
 * tell whether the layout is wrong or the query is.
 *
 * So this is a **simulation**, not a random-row generator. It steps through 180
 * days, signs people up, starts their trials, lets those trials run out,
 * converts some of them, charges them, renews or lapses them, and writes a
 * payment row for every attempt. Revenue, MRR, the funnel and the timeseries
 * are then all derived from the same events — which is exactly how they relate
 * in the real database.
 *
 * It is seeded, so the numbers are identical on every reload. A dashboard whose
 * figures reshuffle each time you refresh is impossible to design against.
 */

// --- Determinism -------------------------------------------------------------

import { PLANS as CATALOGUE, PLAN_LIMITS as LIMITS, SELLABLE as SELLABLE_TIERS } from "../plans.js";

/**
 * mulberry32 — small, fast, and good enough for fixtures.
 *
 * `Math.random()` is what makes a mock dataset useless for design work: the
 * layout you just fixed for a 7-digit revenue figure re-renders with 4 digits
 * and you never see the bug again.
 */
function makeRandom(seed) {
  let state = seed >>> 0;
  return function random() {
    state |= 0;
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const random = makeRandom(20260826);

const pick = (list) => list[Math.floor(random() * list.length)];
const between = (min, max) => min + random() * (max - min);
const intBetween = (min, max) => Math.floor(between(min, max + 1));
const chance = (probability) => random() < probability;

/** RFC-4122-shaped, but from the seeded generator so ids are stable too. */
function uuid() {
  const hex = "0123456789abcdef";
  let out = "";
  for (let i = 0; i < 32; i += 1) {
    if (i === 12) out += "4";
    else if (i === 16) out += hex[(Math.floor(random() * 16) & 0x3) | 0x8];
    else out += hex[Math.floor(random() * 16)];
  }
  return `${out.slice(0, 8)}-${out.slice(8, 12)}-${out.slice(12, 16)}-${out.slice(
    16,
    20
  )}-${out.slice(20)}`;
}

// --- The plan catalogue ------------------------------------------------------
//
// Re-exported from `lib/plans.js` rather than redefined, so the fixture and the
// console can never disagree about what Synapse costs.

export { PLANS, PLAN_LIMITS, SELLABLE } from "../plans.js";

// --- Source material for names ----------------------------------------------

const FIRST_NAMES = [
  "Wanjiru", "Brian", "Achieng", "Kevin", "Njeri", "Dennis", "Amina", "Collins",
  "Faith", "Otieno", "Mercy", "Kipchoge", "Sharon", "Mutiso", "Grace", "Elvis",
  "Cynthia", "Baraka", "Nasra", "Peter", "Halima", "Victor", "Chebet", "Samuel",
  "Zawadi", "Alex", "Nyambura", "Emmanuel", "Sylvia", "Kimani", "Joy", "Abdi",
  "Lydia", "Maina", "Esther", "Tony", "Wangeci", "Erick", "Purity", "Musa",
];

const LAST_NAMES = [
  "Kamau", "Ochieng", "Mwangi", "Otieno", "Wafula", "Njoroge", "Kariuki", "Omondi",
  "Mutua", "Chepkoech", "Kiplagat", "Abdullahi", "Karanja", "Wekesa", "Odhiambo",
  "Njuguna", "Cheruiyot", "Barasa", "Gitau", "Maina", "Mburu", "Waweru", "Onyango",
  "Kilonzo", "Rotich", "Muriuki", "Kibet", "Nyaga", "Owino", "Simiyu",
];

const INSTITUTIONS = [
  ["University of Nairobi", 0.19],
  ["Jomo Kenyatta University of Agriculture and Technology", 0.16],
  ["Kenyatta University", 0.14],
  ["Moi University", 0.1],
  ["Strathmore University", 0.09],
  ["Technical University of Kenya", 0.07],
  ["Egerton University", 0.06],
  ["Maseno University", 0.06],
  ["Multimedia University of Kenya", 0.05],
  ["United States International University", 0.04],
  ["Dedan Kimathi University of Technology", 0.04],
];

const PROGRAMS = [
  "BSc Computer Science", "BSc Actuarial Science", "Bachelor of Commerce",
  "BSc Electrical Engineering", "Bachelor of Laws", "BSc Nursing",
  "BSc Mathematics", "Bachelor of Education", "BSc Civil Engineering",
  "BSc Biochemistry", "Bachelor of Pharmacy", "BSc Economics",
  "BSc Information Technology", "Bachelor of Architecture", "BSc Agriculture",
];

const UNIT_CODES = [
  "CS201", "MAT204", "PHY101", "STA210", "ECO205", "LAW140", "BIO230", "CHE118",
  "ENG222", "ACC201", "MKT310", "PSY104", "CIV260", "MED150", "ICS330",
];

const MATERIAL_TITLES = [
  "Week 4 lecture slides", "Past paper 2024", "Tutorial sheet 3", "Lab manual",
  "Revision notes", "CAT 1 marking scheme", "Course outline", "Reference chapter",
  "Seminar handout", "Assignment brief", "Scanned notes", "Practice questions",
  "Supplementary reading", "Field report template", "Exam past paper 2023",
];

function weightedInstitution() {
  let roll = random();
  for (const [name, weight] of INSTITUTIONS) {
    roll -= weight;
    if (roll <= 0) return name;
  }
  return INSTITUTIONS[0][0];
}

const INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
function inviteCode() {
  let out = "";
  for (let i = 0; i < 8; i += 1) out += INVITE_ALPHABET[Math.floor(random() * INVITE_ALPHABET.length)];
  return out;
}

// --- Calendar ----------------------------------------------------------------

const DAYS = 180;
const DAY_MS = 24 * 60 * 60 * 1000;

/** Midnight today, so "day 0" is a stable boundary rather than "now". */
const TODAY = (() => {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
})();

const START = new Date(TODAY.getTime() - (DAYS - 1) * DAY_MS);

/** A timestamp at a plausible hour on the given simulation day. */
function momentOn(dayIndex, hourHint) {
  const hour = hourHint ?? intBetween(7, 23);
  return new Date(
    START.getTime() + dayIndex * DAY_MS + hour * 3600_000 + intBetween(0, 59) * 60_000
  );
}

const iso = (date) => date.toISOString();

// --- The simulation ----------------------------------------------------------

const users = [];
const payments = [];
const groups = [];
const groupMembers = [];
const devices = [];
const materials = [];
const usageCounters = [];

/** Signups per day: a rising trend, quieter at weekends, with noise. */
function signupsOn(dayIndex) {
  const date = new Date(START.getTime() + dayIndex * DAY_MS);
  const weekday = date.getDay();
  const weekendDrag = weekday === 0 || weekday === 6 ? 0.55 : 1;
  // Term-time bump: a burst around the middle of the window, the way a
  // semester start actually looks.
  const termBump = dayIndex > 96 && dayIndex < 126 ? 1.7 : 1;
  const trend = 1.1 + dayIndex * 0.055;
  return Math.max(0, Math.round(trend * weekendDrag * termBump * between(0.55, 1.5)));
}

/** How a converting student picks a plan. */
function pickPaidTier() {
  const roll = random();
  if (roll < 0.52) return "standard";
  if (roll < 0.86) return "pro";
  return "friends";
}

/**
 * The outcome of one charge attempt.
 *
 * `pending` only happens in the last few days, because a pending charge older
 * than that is precisely the thing the reconcile queue exists to clear — and
 * having a handful of them is what makes that screen worth designing.
 */
function chargeStatus(dayIndex) {
  const roll = random();
  if (roll < 0.885) return "success";
  if (roll < 0.945) return "failed";
  if (roll < 0.98) return "abandoned";
  return dayIndex > DAYS - 6 ? "pending" : "failed";
}

const CHANNELS = [
  ["mobile_money", 0.78],
  ["card", 0.17],
  ["bank", 0.05],
];

function pickChannel() {
  let roll = random();
  for (const [name, weight] of CHANNELS) {
    roll -= weight;
    if (roll <= 0) return name;
  }
  return "mobile_money";
}

function makeUser(dayIndex) {
  const first = pick(FIRST_NAMES);
  const last = pick(LAST_NAMES);
  const created = momentOn(dayIndex);
  const hasEmail = chance(0.38);

  const user = {
    id: uuid(),
    phone: `+2547${intBetween(10, 99)}${String(intBetween(0, 999999)).padStart(6, "0")}`,
    email: hasEmail
      ? `${first.toLowerCase()}.${last.toLowerCase()}${intBetween(1, 99)}@gmail.com`
      : null,
    full_name: `${first} ${last}`,
    institution: weightedInstitution(),
    program: pick(PROGRAMS),
    year_of_study: intBetween(1, 5),
    semester: intBetween(1, 2),
    avatar_path: null,
    created_at: iso(created),
    updated_at: iso(created),
    deleted_at: null,
    active_device_id: null,
    _createdDay: dayIndex,
    // Simulation state, stripped from the API responses further down.
    _sub: null,
    _decisionDay: dayIndex + CATALOGUE.trial.duration_days,
    _paid: 0,
  };

  user._sub = {
    id: uuid(),
    user_id: user.id,
    tier: "trial",
    started_at: iso(created),
    expires_at: iso(new Date(created.getTime() + CATALOGUE.trial.duration_days * DAY_MS)),
    verified: true,
    group_id: null,
  };

  const deviceCount = chance(0.16) ? 2 : 1;
  for (let i = 0; i < deviceCount; i += 1) {
    const device = {
      id: uuid(),
      user_id: user.id,
      platform: chance(0.74) ? "android" : "ios",
      app_version: pick(["1.2.0", "1.3.1", "1.4.0", "1.4.2"]),
      push_token: chance(0.72) ? `ExponentPushToken[${uuid().slice(0, 18)}]` : null,
      created_at: iso(momentOn(dayIndex)),
      updated_at: iso(momentOn(Math.min(DAYS - 1, dayIndex + intBetween(0, 40)))),
    };
    devices.push(device);
    if (i === deviceCount - 1) user.active_device_id = device.id;
  }

  users.push(user);
  return user;
}

/**
 * Attempts a charge and, if it lands, puts the account on the plan.
 *
 * Returns whether the plan was actually activated — a failed charge leaves the
 * student exactly where they were, which is what produces the lapsed accounts
 * with payment history that support screens have to make sense of.
 */
function attemptPurchase(user, tier, dayIndex) {
  const plan = CATALOGUE[tier];
  const at = momentOn(dayIndex);
  const status = chargeStatus(dayIndex);

  payments.push({
    id: uuid(),
    user_id: user.id,
    reference: `als_${at.getTime().toString(36)}${Math.floor(random() * 1e6).toString(36)}`,
    tier,
    amount_kes: plan.price_ksh,
    status,
    channel: status === "success" ? pickChannel() : pickChannel(),
    paid_at: status === "success" ? iso(at) : null,
    created_at: iso(at),
  });

  if (status !== "success") return false;

  user._paid += plan.price_ksh;

  const current = user._sub;
  const currentEnd = current ? new Date(current.expires_at) : null;
  const sameTier = current && current.tier === tier;
  const stillLive = currentEnd && currentEnd > at;
  const base = sameTier && stillLive ? currentEnd : at;
  const expires = new Date(base.getTime() + plan.duration_days * DAY_MS);

  user._sub = {
    id: current ? current.id : uuid(),
    user_id: user.id,
    tier,
    started_at: sameTier && current ? current.started_at : iso(at),
    expires_at: iso(expires),
    // A small share of successful charges never get their webhook. The
    // subscription exists because the app wrote it optimistically; nothing has
    // confirmed the money. This is the reconciliation queue, and it is the
    // single most important state for this console to surface.
    verified: !chance(0.028),
    group_id: null,
  };

  if (tier === "friends") {
    const group = {
      id: uuid(),
      owner_id: user.id,
      tier: "friends",
      seats: CATALOGUE.friends.seats,
      invite_code: inviteCode(),
      expires_at: iso(expires),
      created_at: iso(at),
    };
    groups.push(group);
    groupMembers.push({ group_id: group.id, user_id: user.id, created_at: iso(at) });
    user._sub.group_id = group.id;
    user._ownedGroup = group.id;
  }

  return true;
}

/** Seats a few friends on a group that was just bought. */
function fillGroup(group, dayIndex) {
  const wanted = intBetween(1, 4);
  const candidates = users.filter(
    (candidate) =>
      candidate.id !== group.owner_id &&
      candidate._createdDay <= dayIndex &&
      candidate._sub &&
      candidate._sub.tier !== "friends" &&
      new Date(candidate._sub.expires_at) <= new Date(momentOn(dayIndex))
  );

  for (let i = 0; i < wanted && candidates.length; i += 1) {
    const index = Math.floor(random() * candidates.length);
    const joiner = candidates.splice(index, 1)[0];
    const at = momentOn(dayIndex + intBetween(0, 2));

    groupMembers.push({ group_id: group.id, user_id: joiner.id, created_at: iso(at) });
    joiner._sub = {
      id: joiner._sub.id,
      user_id: joiner.id,
      tier: "friends",
      started_at: iso(at),
      expires_at: group.expires_at,
      verified: true,
      group_id: group.id,
    };
    // A seat ends when the group's own period does, so their next decision is
    // the group's expiry, not their own old one.
    joiner._decisionDay = Math.round(
      (new Date(group.expires_at).getTime() - START.getTime()) / DAY_MS
    );
  }
}

// Step through the calendar.
for (let day = 0; day < DAYS; day += 1) {
  const newcomers = signupsOn(day);
  for (let i = 0; i < newcomers; i += 1) makeUser(day);

  const groupsBoughtToday = [];

  for (const user of users) {
    if (user._createdDay > day) continue;
    if (user._decisionDay !== day) continue;

    const tier = user._sub ? user._sub.tier : "expired";

    if (tier === "trial") {
      // The trial ran out. Roughly a fifth buy something.
      if (chance(0.21)) {
        const chosen = pickPaidTier();
        const bought = attemptPurchase(user, chosen, day);
        if (bought && chosen === "friends") {
          groupsBoughtToday.push(groups[groups.length - 1]);
        }
        // A failed charge is usually retried within a couple of days.
        user._decisionDay = bought ? day + CATALOGUE[chosen].duration_days : day + intBetween(1, 3);
      } else {
        user._decisionDay = -1;
        user._lapsedOn = day;
      }
    } else if (SELLABLE_TIERS.includes(tier)) {
      // Renewal. Synapse holds better than Focus, which is what a plan people
      // actually rely on looks like.
      const renewRate = tier === "pro" ? 0.78 : tier === "friends" ? 0.71 : 0.66;
      if (chance(renewRate)) {
        const bought = attemptPurchase(user, tier, day);
        user._decisionDay = bought ? day + CATALOGUE[tier].duration_days : day + intBetween(1, 3);
      } else {
        user._decisionDay = -1;
        user._lapsedOn = day;
      }
    }
  }

  for (const group of groupsBoughtToday) fillGroup(group, day);

  // Lapsed students occasionally come back — usually when exams are near.
  for (const user of users) {
    if (user._decisionDay !== -1) continue;
    if (user._createdDay > day) continue;
    if (chance(0.004)) {
      const chosen = pickPaidTier();
      const bought = attemptPurchase(user, chosen, day);
      user._decisionDay = bought ? day + CATALOGUE[chosen].duration_days : -1;
      if (bought && chosen === "friends") fillGroup(groups[groups.length - 1], day);
    }
  }
}

// --- Content -----------------------------------------------------------------

const EXTRACTION_WEIGHTS = [
  ["done", 0.9],
  ["pending", 0.045],
  ["failed", 0.032],
  ["running", 0.013],
  ["skipped", 0.01],
];

function pickExtraction() {
  let roll = random();
  for (const [status, weight] of EXTRACTION_WEIGHTS) {
    roll -= weight;
    if (roll <= 0) return status;
  }
  return "done";
}

const EXTRACTION_ERRORS = [
  "No text layer — the PDF is a scan. Queue it for OCR instead.",
  "Encrypted PDF; no password supplied.",
  "Page 41 could not be parsed (malformed xref table).",
  "File exceeded the per-file page limit after extraction.",
];

for (const user of users) {
  // Only accounts that got somewhere file anything.
  const filed = intBetween(0, user._paid > 0 ? 9 : 4);
  for (let i = 0; i < filed; i += 1) {
    const day = intBetween(user._createdDay, DAYS - 1);
    const kind = pick(["pdf", "pdf", "pdf", "note", "image", "link"]);
    const status = kind === "pdf" || kind === "image" ? pickExtraction() : "skipped";
    const pages = kind === "pdf" ? intBetween(4, 180) : null;

    materials.push({
      id: uuid(),
      user_id: user.id,
      unit_id: uuid(),
      unit_code: pick(UNIT_CODES),
      kind,
      title: `${pick(UNIT_CODES)} — ${pick(MATERIAL_TITLES)}`,
      byte_size: kind === "note" || kind === "link" ? null : intBetween(180_000, 42_000_000),
      page_count: status === "done" ? pages : null,
      extraction_status: status,
      extraction_error: status === "failed" ? pick(EXTRACTION_ERRORS) : null,
      created_at: iso(momentOn(day)),
    });
  }
}

// A handful of extractions deliberately stuck in `pending` for days. This is
// what a wedged worker looks like, and the ops page exists to catch it.
for (let i = 0; i < 7; i += 1) {
  const material = materials[Math.floor(random() * materials.length)];
  material.extraction_status = "pending";
  material.created_at = iso(momentOn(DAYS - 1 - intBetween(1, 4)));
}

// --- Usage counters ----------------------------------------------------------

const todayKey = TODAY.toISOString().slice(0, 10);
const monthKey = todayKey.slice(0, 7);

function isoWeekKey(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const dayNumber = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNumber);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d - yearStart) / DAY_MS + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

const weekKey = isoWeekKey(TODAY);

for (const user of users) {
  const active = user._sub && new Date(user._sub.expires_at) > TODAY && user._sub.verified;
  if (!active || !chance(0.55)) continue;

  const limits = LIMITS[user._sub.tier] ?? LIMITS.trial;
  usageCounters.push({
    user_id: user.id,
    metric: "ai_queries",
    period_key: todayKey,
    count: intBetween(0, Math.max(1, Math.round(limits.daily_ai_queries * 1.05))),
  });
  if (chance(0.6)) {
    usageCounters.push({
      user_id: user.id,
      metric: limits.quiz_interval === "weekly" ? "quizzes_weekly" : "quizzes_lifetime",
      period_key: limits.quiz_interval === "weekly" ? weekKey : "lifetime",
      count: intBetween(0, 6),
    });
  }
  if (chance(0.5)) {
    usageCounters.push({
      user_id: user.id,
      metric: "pdf_pages",
      period_key: "lifetime",
      count: intBetween(10, Math.max(20, limits.total_pdf_pages_pool)),
    });
  }
  if (limits.allow_ocr_scans && chance(0.35)) {
    usageCounters.push({
      user_id: user.id,
      metric: "ocr_pages",
      period_key: monthKey,
      count: intBetween(0, 32),
    });
  }
}

// --- A few deleted accounts --------------------------------------------------

for (let i = 0; i < 14; i += 1) {
  const user = users[Math.floor(random() * users.length)];
  if (user.deleted_at) continue;
  user.deleted_at = iso(momentOn(intBetween(user._createdDay, DAYS - 1)));
  user.active_device_id = null;
}

// --- Admins ------------------------------------------------------------------

export const admins = [
  {
    id: uuid(),
    email: "ceo@ardena.co.ke",
    full_name: "Deon Kimathi",
    role: "owner",
    is_active: true,
    last_login_at: iso(momentOn(DAYS - 1, 8)),
    created_at: iso(momentOn(2, 9)),
  },
  {
    id: uuid(),
    email: "grace@ardena.co.ke",
    full_name: "Grace Wanjiku",
    role: "admin",
    is_active: true,
    last_login_at: iso(momentOn(DAYS - 1, 10)),
    created_at: iso(momentOn(31, 11)),
  },
  {
    id: uuid(),
    email: "support@ardena.co.ke",
    full_name: "Brian Otieno",
    role: "support",
    is_active: true,
    last_login_at: iso(momentOn(DAYS - 2, 15)),
    created_at: iso(momentOn(64, 14)),
  },
  {
    id: uuid(),
    email: "intern@ardena.co.ke",
    full_name: "Faith Njeri",
    role: "support",
    is_active: false,
    last_login_at: iso(momentOn(DAYS - 40, 12)),
    created_at: iso(momentOn(96, 9)),
  },
];

// --- Audit log ---------------------------------------------------------------

const AUDIT_TEMPLATES = [
  {
    action: "admin.signed_in",
    target_type: "admin",
    weight: 0.34,
    summary: (admin) => `${admin.email} signed in`,
  },
  {
    action: "user.device_reset",
    target_type: "user",
    weight: 0.22,
    summary: (admin, user) => `Released device lock for ${user.full_name} — new phone`,
  },
  {
    action: "subscription.granted",
    target_type: "user",
    weight: 0.14,
    summary: (admin, user) =>
      `Granted Synapse to ${user.full_name} — compensation for the sync outage`,
  },
  {
    action: "payment.reconciled",
    target_type: "payment",
    weight: 0.12,
    summary: () => "Reconciled a pending charge: pending -> success, activated pro",
  },
  {
    action: "user.updated",
    target_type: "user",
    weight: 0.07,
    summary: (admin, user) => `Edited full_name, institution on ${user.full_name}`,
  },
  {
    action: "usage.reset",
    target_type: "user",
    weight: 0.05,
    summary: (admin, user) =>
      `Cleared ai_queries usage counters (1 row) for ${user.full_name} — our fault`,
  },
  {
    action: "user.deleted",
    target_type: "user",
    weight: 0.03,
    summary: (admin, user) => `Deleted ${user.full_name} — requested by the student`,
  },
  {
    action: "subscription.revoked",
    target_type: "user",
    weight: 0.02,
    summary: (admin, user) => `Ended Focus for ${user.full_name} — duplicate account`,
  },
  {
    action: "admin.created",
    target_type: "admin",
    weight: 0.01,
    summary: () => "Created support brian@ardena.co.ke",
  },
];

function pickTemplate() {
  let roll = random();
  for (const template of AUDIT_TEMPLATES) {
    roll -= template.weight;
    if (roll <= 0) return template;
  }
  return AUDIT_TEMPLATES[0];
}

export const auditLog = [];
for (let i = 0; i < 168; i += 1) {
  const template = pickTemplate();
  const admin = pick(admins.filter((a) => a.is_active));
  const user = pick(users);
  const day = intBetween(DAYS - 60, DAYS - 1);

  auditLog.push({
    id: uuid(),
    admin_id: admin.id,
    admin_email: admin.email,
    action: template.action,
    target_type: template.target_type,
    target_id: template.target_type === "user" ? user.id : admin.id,
    summary: template.summary(admin, user),
    meta:
      template.action === "subscription.granted"
        ? {
            before: { tier: "expired", verified: false },
            after: { tier: "pro", verified: true },
            reason: "compensation for the sync outage",
          }
        : null,
    ip: `41.90.${intBetween(1, 254)}.${intBetween(1, 254)}`,
    created_at: iso(momentOn(day)),
  });
}
auditLog.sort((a, b) => b.created_at.localeCompare(a.created_at));

// --- Publish ------------------------------------------------------------------
//
// The simulation state (`_sub`, `_decisionDay`, …) stays on the objects because
// the mock server derives from it, but nothing under `_` is ever serialised into
// a response — see `server.js`.

export const db = {
  users,
  payments,
  groups,
  groupMembers,
  devices,
  materials,
  usageCounters,
  admins,
  auditLog,
  today: TODAY,
  start: START,
  days: DAYS,
};

export { DAY_MS, TODAY, START, DAYS };
