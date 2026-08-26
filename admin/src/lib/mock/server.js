/*
 * The mock API.
 *
 * Every function here returns **exactly** the shape the FastAPI service returns
 * for the same route. That is the whole discipline of this file: the moment a
 * mock returns a convenient shape rather than the real one, the console gets
 * built against a contract that does not exist, and switching to the live API
 * becomes a rewrite instead of a flag.
 *
 * The aggregate logic mirrors `app/services/analytics.py` — including the part
 * that is easy to get wrong. A Friends plan is one payment and up to five
 * entitled people, so MRR counts it **per group** while seats count per person.
 * If this file summed the plan price across subscribers it would report five
 * times the money that exists, and the dashboard would be designed around a
 * number that can never be true.
 */

import { DAY_MS, PLAN_LIMITS, PLANS, SELLABLE, TODAY, db } from "./dataset.js";

const MONTH_DAYS = 30;

// --- Helpers -----------------------------------------------------------------

const now = () => new Date();

/** Strips the simulation's private fields before anything leaves this module. */
function publicUser(user) {
  const out = {};
  for (const [key, value] of Object.entries(user)) {
    if (!key.startsWith("_")) out[key] = value;
  }
  return out;
}

function daysRemaining(expiresAt) {
  if (!expiresAt) return null;
  return Math.max(0, Math.floor((new Date(expiresAt) - now()) / DAY_MS));
}

function isLive(sub) {
  return Boolean(sub && sub.verified && sub.expires_at && new Date(sub.expires_at) > now());
}

/**
 * The tier actually in force, computed the same way `get_entitlement` does.
 *
 * A lapsed subscription resolves to `expired`, not back to trial. An unverified
 * paid one resolves to `expired` too — the app writes it on the student's word,
 * and a claim is not an entitlement. Showing this beside the raw subscription
 * row is the whole reason the user detail screen is useful to support.
 */
function entitlement(user) {
  const sub = user._sub;
  if (!sub) return { tier: "expired", nominal: "expired" };
  const nominal = PLANS[sub.tier] ? sub.tier : "expired";
  const lapsed = !sub.expires_at || new Date(sub.expires_at) <= now();
  const unverifiedPaid = nominal !== "trial" && !sub.verified;
  return { tier: lapsed || unverifiedPaid ? "expired" : nominal, nominal };
}

const liveUsers = () => db.users.filter((u) => !u.deleted_at);
const successful = () => db.payments.filter((p) => p.status === "success");

function sumAmount(rows) {
  return rows.reduce((total, row) => total + row.amount_kes, 0);
}

function since(days) {
  return new Date(now().getTime() - days * DAY_MS);
}

function paidTotalFor(userId) {
  return sumAmount(db.payments.filter((p) => p.user_id === userId && p.status === "success"));
}

function seatsTaken(groupId) {
  return db.groupMembers.filter((m) => m.group_id === groupId).length;
}

function page(items, limit, offset) {
  return {
    items: items.slice(offset, offset + limit),
    total: items.length,
    limit,
    offset,
  };
}

// --- Aggregates ---------------------------------------------------------------

function userCounts() {
  const live = liveUsers();
  return {
    total: live.length,
    deleted: db.users.length - live.length,
    new_today: live.filter((u) => new Date(u.created_at) >= since(1)).length,
    new_7d: live.filter((u) => new Date(u.created_at) >= since(7)).length,
    new_30d: live.filter((u) => new Date(u.created_at) >= since(30)).length,
    with_devices: new Set(db.devices.map((d) => d.user_id)).size,
  };
}

function planBreakdown() {
  const moment = now();
  const liveGroups = db.groups.filter(
    (g) => g.expires_at && new Date(g.expires_at) > moment
  ).length;

  return ["trial", ...SELLABLE].map((tier) => {
    const plan = PLANS[tier];
    const held = db.users.filter((u) => u._sub && u._sub.tier === tier);
    const active = held.filter((u) => isLive(u._sub));

    const revenueAll = sumAmount(successful().filter((p) => p.tier === tier));
    const revenue30 = sumAmount(
      successful().filter((p) => p.tier === tier && new Date(p.created_at) >= since(30))
    );

    // What actually bills. A Friends group is one payment however many seats it
    // seats; a seat borrowed from someone else's group bills nothing.
    let billable;
    if (tier === "friends") billable = liveGroups;
    else if (tier === "trial") billable = 0;
    else billable = active.filter((u) => !u._sub.group_id).length;

    return {
      tier,
      name: plan.name,
      price_ksh: plan.price_ksh,
      subscribers: held.length,
      active: active.length,
      paying: tier === "trial" ? 0 : active.length,
      unverified: held.filter(
        (u) => !u._sub.verified && new Date(u._sub.expires_at) > moment
      ).length,
      expiring_7d: active.filter(
        (u) => new Date(u._sub.expires_at) <= new Date(moment.getTime() + 7 * DAY_MS)
      ).length,
      revenue_all_time_ksh: revenueAll,
      revenue_30d_ksh: revenue30,
      mrr_ksh: Math.round((billable * plan.price_ksh * MONTH_DAYS) / Math.max(1, plan.duration_days)),
    };
  });
}

function revenueSummary() {
  const paid = successful();
  const gross = sumAmount(paid);
  const last30 = sumAmount(paid.filter((p) => new Date(p.created_at) >= since(30)));
  const previous30 = sumAmount(
    paid.filter((p) => {
      const at = new Date(p.created_at);
      return at >= since(60) && at < since(30);
    })
  );

  const byStatus = {};
  for (const payment of db.payments) {
    byStatus[payment.status] = (byStatus[payment.status] ?? 0) + 1;
  }
  const attempted = Object.values(byStatus).reduce((a, b) => a + b, 0);
  const succeeded = byStatus.success ?? 0;

  const byChannel = {};
  for (const payment of paid) {
    const key = payment.channel ?? "unknown";
    byChannel[key] = (byChannel[key] ?? 0) + payment.amount_kes;
  }

  const plans = planBreakdown();
  const paying = plans.reduce((total, row) => total + row.paying, 0);

  return {
    currency: "KES",
    gross_ksh: gross,
    today_ksh: sumAmount(paid.filter((p) => new Date(p.created_at) >= since(1))),
    last_7d_ksh: sumAmount(paid.filter((p) => new Date(p.created_at) >= since(7))),
    last_30d_ksh: last30,
    previous_30d_ksh: previous30,
    // Null, not zero, when there is nothing to compare against. "+100%" on a
    // first month is a lie the chart would go on repeating.
    growth_30d_pct: previous30
      ? Math.round(((last30 - previous30) / previous30) * 1000) / 10
      : null,
    mrr_ksh: plans.reduce((total, row) => total + row.mrr_ksh, 0),
    arpu_ksh: paying ? Math.round(last30 / paying) : 0,
    paying_customers: paying,
    successful_payments: succeeded,
    failed_payments: (byStatus.failed ?? 0) + (byStatus.abandoned ?? 0),
    pending_payments: byStatus.pending ?? 0,
    success_rate_pct: attempted ? Math.round((succeeded / attempted) * 1000) / 10 : 0,
    average_payment_ksh: succeeded ? Math.round(gross / succeeded) : 0,
    by_channel: byChannel,
  };
}

function funnel() {
  const moment = now();
  const signedUp = liveUsers().length;
  const startedTrial = db.users.filter((u) => u._sub).length;
  const trialActive = db.users.filter((u) => u._sub?.tier === "trial" && isLive(u._sub)).length;
  const trialExpired = db.users.filter(
    (u) => u._sub?.tier === "trial" && new Date(u._sub.expires_at) <= moment
  ).length;
  const everPaid = new Set(successful().map((p) => p.user_id)).size;
  const payingNow = db.users.filter(
    (u) => u._sub && SELLABLE.includes(u._sub.tier) && isLive(u._sub)
  ).length;

  const decided = trialExpired + everPaid;
  return {
    signed_up: signedUp,
    started_trial: startedTrial,
    trial_active: trialActive,
    trial_expired: trialExpired,
    ever_paid: everPaid,
    paying_now: payingNow,
    trial_conversion_pct: decided ? Math.round((everPaid / decided) * 1000) / 10 : 0,
    retention_pct: everPaid ? Math.round((payingNow / everPaid) * 1000) / 10 : 0,
  };
}

function contentStats() {
  const live = db.materials;
  const extraction = {};
  for (const material of live) {
    extraction[material.extraction_status] = (extraction[material.extraction_status] ?? 0) + 1;
  }

  const hourAgo = new Date(now().getTime() - 3600_000);
  const messages = Math.round(live.length * 6.4);

  return {
    units: Math.round(live.length * 0.34),
    materials: live.length,
    material_chunks: Math.round(live.length * 21.6),
    storage_bytes: live.reduce((total, m) => total + (m.byte_size ?? 0), 0),
    events: Math.round(live.length * 0.48),
    class_sessions: Math.round(live.length * 0.29),
    chats: Math.round(live.length * 0.72),
    messages,
    tutor_answers: Math.round(messages * 0.5),
    prompt_tokens: messages * 1180,
    completion_tokens: messages * 320,
    extraction,
    extraction_stalled: live.filter(
      (m) => m.extraction_status === "pending" && new Date(m.created_at) < hourAgo
    ).length,
  };
}

// --- Time series --------------------------------------------------------------

const SERIES = {
  signups: () => liveUsers().map((u) => ({ at: u.created_at, value: 1 })),
  revenue: () => successful().map((p) => ({ at: p.created_at, value: p.amount_kes })),
  payments: () => successful().map((p) => ({ at: p.created_at, value: 1 })),
  failed_payments: () =>
    db.payments
      .filter((p) => p.status === "failed" || p.status === "abandoned")
      .map((p) => ({ at: p.created_at, value: 1 })),
  materials: () => db.materials.map((m) => ({ at: m.created_at, value: 1 })),
  questions: () =>
    db.materials.flatMap((m) =>
      Array.from({ length: 3 }, () => ({ at: m.created_at, value: 1 }))
    ),
  active_students: () =>
    db.materials.map((m) => ({ at: m.created_at, value: 1, distinct: m.user_id })),
};

/**
 * A dense daily series — every day in the window, zeros included.
 *
 * The empty days are the point. A line that skips them slopes straight through
 * the outage that produced them, which is precisely the shape a dashboard is
 * supposed to make visible.
 */
function timeseries(metric, days) {
  const source = SERIES[metric] ?? SERIES.signups;
  const buckets = new Map();
  const distinct = new Map();

  const startKey = new Date(TODAY.getTime() - (days - 1) * DAY_MS);
  for (let i = 0; i < days; i += 1) {
    buckets.set(new Date(startKey.getTime() + i * DAY_MS).toISOString().slice(0, 10), 0);
  }

  for (const row of source()) {
    const key = row.at.slice(0, 10);
    if (!buckets.has(key)) continue;

    if (row.distinct) {
      if (!distinct.has(key)) distinct.set(key, new Set());
      distinct.get(key).add(row.distinct);
      buckets.set(key, distinct.get(key).size);
    } else {
      buckets.set(key, buckets.get(key) + row.value);
    }
  }

  const points = [...buckets.entries()].map(([day, value]) => ({ day, value }));
  return {
    metric,
    days,
    points,
    total: points.reduce((sum, point) => sum + point.value, 0),
  };
}

// --- Attention ----------------------------------------------------------------

function attention() {
  const moment = now();
  const items = [];

  const unverified = db.users.filter(
    (u) =>
      u._sub &&
      SELLABLE.includes(u._sub.tier) &&
      !u._sub.verified &&
      new Date(u._sub.expires_at) > moment
  ).length;

  if (unverified) {
    items.push({
      level: "critical",
      code: "unverified_paid_subscriptions",
      message: `${unverified} paid subscription${unverified === 1 ? "" : "s"} never confirmed by Kora. Each is either a lost payment or a free plan.`,
      count: unverified,
      link: "/subscriptions?verified=false",
    });
  }

  const stalePending = db.payments.filter(
    (p) => p.status === "pending" && new Date(p.created_at) < new Date(moment.getTime() - 3600_000)
  ).length;

  if (stalePending) {
    items.push({
      level: "warn",
      code: "stale_pending_payments",
      message: `${stalePending} payment${stalePending === 1 ? "" : "s"} still pending after an hour. Reconcile against Kora.`,
      count: stalePending,
      link: "/payments?status=pending",
    });
  }

  const content = contentStats();
  if (content.extraction_stalled) {
    items.push({
      level: "warn",
      code: "extraction_stalled",
      message: `${content.extraction_stalled} materials have been waiting over an hour for text extraction.`,
      count: content.extraction_stalled,
      link: "/content?extraction_status=pending",
    });
  }

  const failed = content.extraction.failed ?? 0;
  if (failed) {
    items.push({
      level: "info",
      code: "extraction_failed",
      message: `${failed} material${failed === 1 ? "" : "s"} failed extraction.`,
      count: failed,
      link: "/content?extraction_status=failed",
    });
  }

  const expiring = db.users.filter(
    (u) =>
      u._sub &&
      SELLABLE.includes(u._sub.tier) &&
      isLive(u._sub) &&
      new Date(u._sub.expires_at) <= new Date(moment.getTime() + 3 * DAY_MS)
  ).length;

  if (expiring) {
    items.push({
      level: "info",
      code: "expiring_soon",
      message: `${expiring} paid plan${expiring === 1 ? "" : "s"} expire within three days.`,
      count: expiring,
      link: "/subscriptions?expiring_days=3",
    });
  }

  return items;
}

// --- Row shapes ---------------------------------------------------------------

function userRow(user) {
  const { tier } = entitlement(user);
  const sub = user._sub;
  return {
    id: user.id,
    phone: user.phone,
    email: user.email,
    full_name: user.full_name,
    institution: user.institution,
    created_at: user.created_at,
    tier,
    plan_name: PLANS[tier].name,
    expires_at: sub ? sub.expires_at : null,
    verified: Boolean(sub && sub.verified),
    is_deleted: Boolean(user.deleted_at),
    total_paid_ksh: paidTotalFor(user.id),
  };
}

function subscriptionOut(sub) {
  if (!sub) return null;
  return {
    tier: sub.tier,
    plan_name: PLANS[sub.tier]?.name ?? "Expired",
    started_at: sub.started_at,
    expires_at: sub.expires_at,
    verified: sub.verified,
    group_id: sub.group_id,
    days_remaining: daysRemaining(sub.expires_at),
    is_active: isLive(sub),
  };
}

function userActivity(userId) {
  const owned = db.materials.filter((m) => m.user_id === userId);
  return {
    units: new Set(owned.map((m) => m.unit_code)).size,
    materials: owned.length,
    events: Math.round(owned.length * 0.5),
    class_sessions: Math.round(owned.length * 0.3),
    chats: Math.round(owned.length * 0.7),
    messages: Math.round(owned.length * 6),
    study_days: Math.round(owned.length * 2.4),
    devices: db.devices.filter((d) => d.user_id === userId).length,
    storage_bytes: owned.reduce((total, m) => total + (m.byte_size ?? 0), 0),
  };
}

function usageFor(userId) {
  const grouped = {};
  for (const row of db.usageCounters.filter((c) => c.user_id === userId)) {
    grouped[row.metric] = grouped[row.metric] ?? {};
    grouped[row.metric][row.period_key] = row.count;
  }
  return grouped;
}

function groupRow(group) {
  const owner = db.users.find((u) => u.id === group.owner_id);
  return {
    id: group.id,
    owner_id: group.owner_id,
    owner_name: owner?.full_name ?? "",
    owner_phone: owner?.phone ?? null,
    tier: group.tier,
    invite_code: group.invite_code,
    seats: group.seats,
    seats_taken: seatsTaken(group.id),
    expires_at: group.expires_at,
    is_active: Boolean(group.expires_at && new Date(group.expires_at) > now()),
    created_at: group.created_at,
  };
}

// --- Filtering ----------------------------------------------------------------

function matches(haystack, needle) {
  return (haystack ?? "").toLowerCase().includes(needle);
}

function filterUsers(query) {
  const moment = now();
  let rows = query.status === "deleted" ? db.users.filter((u) => u.deleted_at) : liveUsers();

  if (query.q) {
    const needle = query.q.trim().toLowerCase();
    rows = rows.filter(
      (u) =>
        u.id === needle ||
        matches(u.phone, needle) ||
        matches(u.email, needle) ||
        matches(u.full_name, needle) ||
        matches(u.institution, needle)
    );
  }

  if (query.tier) rows = rows.filter((u) => u._sub?.tier === query.tier);
  if (query.institution) {
    rows = rows.filter(
      (u) => u.institution.toLowerCase() === query.institution.trim().toLowerCase()
    );
  }

  switch (query.status) {
    case "active":
      rows = rows.filter((u) => isLive(u._sub));
      break;
    case "paying":
      rows = rows.filter((u) => isLive(u._sub) && SELLABLE.includes(u._sub.tier));
      break;
    case "trial":
      rows = rows.filter((u) => isLive(u._sub) && u._sub.tier === "trial");
      break;
    case "expired":
      rows = rows.filter(
        (u) => !u._sub || !u._sub.expires_at || new Date(u._sub.expires_at) <= moment
      );
      break;
    case "unverified":
      rows = rows.filter((u) => u._sub && SELLABLE.includes(u._sub.tier) && !u._sub.verified);
      break;
    default:
      break;
  }

  const direction = query.order === "asc" ? 1 : -1;
  const key = query.sort ?? "created_at";
  rows = [...rows].sort((a, b) => {
    if (key === "name") return direction * a.full_name.localeCompare(b.full_name);
    if (key === "expires_at") {
      return direction * String(a._sub?.expires_at ?? "").localeCompare(b._sub?.expires_at ?? "");
    }
    return direction * a.created_at.localeCompare(b.created_at);
  });

  return rows;
}

// --- The route table ----------------------------------------------------------
//
// Keyed by "METHOD /path", with `{param}` segments. Kept as one table rather
// than a chain of ifs so the mock's surface can be read against the backend's
// route list at a glance.

const routes = {
  "POST /auth/login": (_params, body) => {
    const admin = db.admins.find(
      (a) => a.email.toLowerCase() === String(body.email ?? "").trim().toLowerCase()
    );
    // The mock accepts any password for a known, active admin. It is a design
    // fixture, not a security boundary — the real service hashes with scrypt.
    if (!admin) throw httpError(401, "Those details are not right.");
    if (!admin.is_active) throw httpError(403, "That admin account has been deactivated.");

    return {
      access_token: `mock.${admin.id}`,
      refresh_token: `mock-refresh.${admin.id}`,
      token_type: "bearer",
      expires_in: 3600,
      admin,
    };
  },

  "POST /auth/refresh": (_params, body) => {
    const id = String(body.refresh_token ?? "").split(".")[1];
    const admin = db.admins.find((a) => a.id === id) ?? db.admins[0];
    return {
      access_token: `mock.${admin.id}`,
      refresh_token: `mock-refresh.${admin.id}`,
      token_type: "bearer",
      expires_in: 3600,
      admin,
    };
  },

  "POST /auth/logout": () => ({ ok: true, message: "Signed out." }),

  "GET /auth/me": (_params, _body, context) => currentAdmin(context),

  "GET /overview": () => ({
    generated_at: now().toISOString(),
    users: userCounts(),
    revenue: revenueSummary(),
    plans: planBreakdown(),
    funnel: funnel(),
    attention: attention(),
  }),

  "GET /overview/timeseries": (_params, _body, context) =>
    timeseries(context.query.metric ?? "signups", Number(context.query.days ?? 30)),

  "GET /overview/institutions": (_params, _body, context) => {
    const counts = new Map();
    for (const user of liveUsers()) {
      if (!user.institution) continue;
      counts.set(user.institution, (counts.get(user.institution) ?? 0) + 1);
    }
    return [...counts.entries()]
      .map(([institution, count]) => ({ institution, users: count }))
      .sort((a, b) => b.users - a.users)
      .slice(0, Number(context.query.limit ?? 20));
  },

  "GET /users": (_params, _body, context) => {
    const rows = filterUsers(context.query);
    const limit = Number(context.query.limit ?? 50);
    const offset = Number(context.query.offset ?? 0);
    const slice = page(rows, limit, offset);
    return { ...slice, items: slice.items.map(userRow) };
  },

  "GET /users/{id}": (params) => {
    const user = requireUser(params.id);
    const { tier } = entitlement(user);

    return {
      ...publicUser(user),
      subscription: subscriptionOut(user._sub),
      effective_tier: tier,
      effective_plan_name: PLANS[tier].name,
      activity: userActivity(user.id),
      usage: usageFor(user.id),
      limits: PLAN_LIMITS[tier] ?? PLAN_LIMITS.expired,
      payments: db.payments
        .filter((p) => p.user_id === user.id)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 50),
      total_paid_ksh: paidTotalFor(user.id),
      devices: db.devices
        .filter((d) => d.user_id === user.id)
        .map((device) => ({
          id: device.id,
          platform: device.platform,
          app_version: device.app_version,
          has_push_token: Boolean(device.push_token),
          is_active_device: user.active_device_id === device.id,
          created_at: device.created_at,
          updated_at: device.updated_at,
        })),
      groups: db.groupMembers
        .filter((m) => m.user_id === user.id)
        .map((membership) => {
          const group = db.groups.find((g) => g.id === membership.group_id);
          return {
            id: group.id,
            invite_code: group.invite_code,
            seats: group.seats,
            seats_taken: seatsTaken(group.id),
            expires_at: group.expires_at,
            is_owner: group.owner_id === user.id,
          };
        }),
    };
  },

  "PATCH /users/{id}": (params, body) => {
    const user = requireUser(params.id);
    Object.assign(user, body);
    user.updated_at = now().toISOString();
    return routes["GET /users/{id}"]({ id: params.id });
  },

  "POST /users/{id}/subscription": (params, body, context) => {
    const user = requireUser(params.id);
    const plan = PLANS[body.tier];
    if (!plan) throw httpError(400, "Unknown plan.");

    const at = now();
    const currentEnd = user._sub ? new Date(user._sub.expires_at) : null;
    const sameTier = user._sub?.tier === body.tier;
    const stillLive = currentEnd && currentEnd > at;
    const base = body.extend !== false && sameTier && stillLive ? currentEnd : at;
    const length = body.days ?? plan.duration_days;

    user._sub = {
      id: user._sub?.id ?? crypto.randomUUID(),
      user_id: user.id,
      tier: body.tier,
      started_at: sameTier && user._sub ? user._sub.started_at : at.toISOString(),
      expires_at:
        body.tier === "expired"
          ? at.toISOString()
          : new Date(base.getTime() + length * DAY_MS).toISOString(),
      verified: body.tier !== "expired",
      // A comped plan is not a group seat. Leaving a stale group id here would
      // make a granted plan look like a paid group's member and quietly add a
      // person to that group's revenue attribution.
      group_id: null,
    };

    logAction(context, {
      action: "subscription.granted",
      target_type: "user",
      target_id: user.id,
      summary: `Granted ${plan.name} to ${user.full_name} — ${body.reason}`,
      meta: { reason: body.reason, days: body.days ?? null, extend: body.extend !== false },
    });

    return subscriptionOut(user._sub);
  },

  "DELETE /users/{id}/subscription": (params, _body, context) => {
    const user = requireUser(params.id);
    if (!user._sub) throw httpError(404, "That account has no subscription.");

    const wasTier = user._sub.tier;
    user._sub.expires_at = now().toISOString();
    user._sub.verified = false;

    logAction(context, {
      action: "subscription.revoked",
      target_type: "user",
      target_id: user.id,
      summary: `Ended ${PLANS[wasTier]?.name ?? wasTier} for ${user.full_name} — ${context.query.reason}`,
    });

    return { ok: true, message: "Subscription ended." };
  },

  "POST /users/{id}/device-reset": (params, _body, context) => {
    const user = requireUser(params.id);
    user.active_device_id = null;

    logAction(context, {
      action: "user.device_reset",
      target_type: "user",
      target_id: user.id,
      summary: `Released device lock for ${user.full_name} — ${context.query.reason ?? "New device"}`,
    });

    return { ok: true, message: "Device lock released. They can sign in again." };
  },

  "GET /users/{id}/usage": (params) => {
    const user = requireUser(params.id);
    const { tier } = entitlement(user);
    return {
      tier,
      plan_name: PLANS[tier].name,
      counters: usageFor(user.id),
      limits: PLAN_LIMITS[tier] ?? PLAN_LIMITS.expired,
    };
  },

  "POST /users/{id}/usage/reset": (params, body, context) => {
    const user = requireUser(params.id);
    const before = db.usageCounters.length;
    db.usageCounters = db.usageCounters.filter(
      (row) => row.user_id !== user.id || (body.metric && row.metric !== body.metric)
    );
    const cleared = before - db.usageCounters.length;

    logAction(context, {
      action: "usage.reset",
      target_type: "user",
      target_id: user.id,
      summary: `Cleared ${body.metric ?? "all"} usage counters (${cleared} rows) — ${body.reason}`,
    });

    return { ok: true, message: `Cleared ${cleared} counter rows.` };
  },

  "DELETE /users/{id}": (params, _body, context) => {
    const user = requireUser(params.id);
    user.deleted_at = now().toISOString();
    user.active_device_id = null;

    logAction(context, {
      action: "user.deleted",
      target_type: "user",
      target_id: user.id,
      summary: `Deleted ${user.full_name} — ${context.query.reason}`,
    });

    return { ok: true, message: "Account deleted." };
  },

  "POST /users/{id}/restore": (params, _body, context) => {
    const user = requireUser(params.id);
    user.deleted_at = null;

    logAction(context, {
      action: "user.restored",
      target_type: "user",
      target_id: user.id,
      summary: `Restored ${user.full_name} — ${context.query.reason}`,
    });

    return { ok: true, message: "Account restored." };
  },

  "GET /subscriptions": (_params, _body, context) => {
    const { query } = context;
    const moment = now();
    let rows = liveUsers().filter((u) => u._sub);

    if (query.tier) rows = rows.filter((u) => u._sub.tier === query.tier);
    if (query.verified !== undefined) {
      rows = rows.filter((u) => u._sub.verified === (query.verified === "true"));
    }
    if (query.active === "true") rows = rows.filter((u) => isLive(u._sub));
    if (query.active === "false") rows = rows.filter((u) => !isLive(u._sub));
    if (query.expiring_days) {
      const horizon = new Date(moment.getTime() + Number(query.expiring_days) * DAY_MS);
      rows = rows.filter(
        (u) => new Date(u._sub.expires_at) > moment && new Date(u._sub.expires_at) <= horizon
      );
    }
    if (query.q) {
      const needle = query.q.trim().toLowerCase();
      rows = rows.filter(
        (u) => matches(u.full_name, needle) || matches(u.phone, needle) || matches(u.email, needle)
      );
    }

    const direction = query.order === "desc" ? -1 : 1;
    const key = query.sort === "started_at" ? "started_at" : "expires_at";
    rows = [...rows].sort((a, b) => direction * a._sub[key].localeCompare(b._sub[key]));

    const limit = Number(query.limit ?? 50);
    const offset = Number(query.offset ?? 0);
    const slice = page(rows, limit, offset);

    return {
      ...slice,
      items: slice.items.map((user) => ({
        id: user._sub.id,
        user_id: user.id,
        full_name: user.full_name,
        phone: user.phone,
        email: user.email,
        tier: user._sub.tier,
        plan_name: PLANS[user._sub.tier].name,
        started_at: user._sub.started_at,
        expires_at: user._sub.expires_at,
        verified: user._sub.verified,
        is_active: isLive(user._sub),
        days_remaining: daysRemaining(user._sub.expires_at),
        group_id: user._sub.group_id,
      })),
    };
  },

  "GET /subscriptions/stats": () => {
    const moment = now();
    const plans = planBreakdown();
    const withSub = db.users.filter((u) => u._sub);

    return {
      generated_at: moment.toISOString(),
      plans,
      total_active: withSub.filter((u) => isLive(u._sub)).length,
      total_paying: plans.reduce((total, row) => total + row.paying, 0),
      total_trial: withSub.filter((u) => u._sub.tier === "trial" && isLive(u._sub)).length,
      total_expired: withSub.filter((u) => new Date(u._sub.expires_at) <= moment).length,
      total_unverified: withSub.filter(
        (u) =>
          SELLABLE.includes(u._sub.tier) &&
          !u._sub.verified &&
          new Date(u._sub.expires_at) > moment
      ).length,
      mrr_ksh: plans.reduce((total, row) => total + row.mrr_ksh, 0),
    };
  },

  "GET /revenue/summary": () => revenueSummary(),
  "GET /revenue/by-plan": () => planBreakdown(),

  "GET /revenue/timeseries": (_params, _body, context) =>
    timeseries(context.query.metric ?? "revenue", Number(context.query.days ?? 30)),

  "GET /revenue/top-customers": (_params, _body, context) => {
    const windowDays = context.query.days ? Number(context.query.days) : null;
    const cutoff = windowDays ? since(windowDays) : null;
    const totals = new Map();

    for (const payment of successful()) {
      if (cutoff && new Date(payment.created_at) < cutoff) continue;
      const current = totals.get(payment.user_id) ?? { total: 0, count: 0 };
      current.total += payment.amount_kes;
      current.count += 1;
      totals.set(payment.user_id, current);
    }

    return [...totals.entries()]
      .map(([userId, stats]) => {
        const user = db.users.find((u) => u.id === userId);
        return {
          user_id: userId,
          full_name: user?.full_name ?? "(deleted)",
          phone: user?.phone ?? null,
          institution: user?.institution ?? "",
          total_paid_ksh: stats.total,
          payments: stats.count,
        };
      })
      .sort((a, b) => b.total_paid_ksh - a.total_paid_ksh)
      .slice(0, Number(context.query.limit ?? 20));
  },

  "GET /payments": (_params, _body, context) => {
    const { query } = context;
    let rows = db.payments;

    if (query.status) rows = rows.filter((p) => p.status === query.status);
    if (query.tier) rows = rows.filter((p) => p.tier === query.tier);
    if (query.channel) rows = rows.filter((p) => p.channel === query.channel);
    if (query.q) {
      const needle = query.q.trim().toLowerCase();
      rows = rows.filter((payment) => {
        const user = db.users.find((u) => u.id === payment.user_id);
        return (
          matches(payment.reference, needle) ||
          matches(user?.full_name, needle) ||
          matches(user?.phone, needle)
        );
      });
    }

    rows = [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at));
    const limit = Number(query.limit ?? 50);
    const offset = Number(query.offset ?? 0);
    const slice = page(rows, limit, offset);

    return {
      ...slice,
      items: slice.items.map((payment) => {
        const user = db.users.find((u) => u.id === payment.user_id);
        return { ...payment, full_name: user?.full_name ?? "", phone: user?.phone ?? null };
      }),
    };
  },

  "GET /payments/{id}": (params) => {
    const payment = db.payments.find((p) => p.id === params.id);
    if (!payment) throw httpError(404, "No payment with that id.");
    const user = db.users.find((u) => u.id === payment.user_id);
    return {
      payment,
      user: user
        ? { id: user.id, full_name: user.full_name, phone: user.phone, email: user.email }
        : null,
    };
  },

  "POST /payments/{reference}/reconcile": (params, _body, context) => {
    const payment = db.payments.find((p) => p.reference === params.reference);
    if (!payment) throw httpError(404, "No payment with that reference.");

    const was = payment.status;
    // Kora usually confirms a stuck pending charge — that is the whole
    // reason the queue is worth working through.
    const confirmed = was === "pending" ? Math.random() < 0.8 : was === "success";

    if (confirmed) {
      payment.status = "success";
      payment.paid_at = payment.paid_at ?? now().toISOString();
      const user = db.users.find((u) => u.id === payment.user_id);
      if (user) {
        const plan = PLANS[payment.tier];
        user._sub = {
          id: user._sub?.id ?? crypto.randomUUID(),
          user_id: user.id,
          tier: payment.tier,
          started_at: now().toISOString(),
          expires_at: new Date(now().getTime() + plan.duration_days * DAY_MS).toISOString(),
          verified: true,
          group_id: null,
        };
      }
    } else {
      payment.status = "failed";
    }

    logAction(context, {
      action: "payment.reconciled",
      target_type: "payment",
      target_id: payment.id,
      summary: `Reconciled ${payment.reference}: ${was} -> ${payment.status}`,
    });

    return confirmed
      ? { ok: true, message: `Confirmed. The student is now on ${payment.tier}.` }
      : { ok: false, message: `Kora reports this payment as failed. Nothing granted.` };
  },

  "GET /groups": (_params, _body, context) => {
    const { query } = context;
    let rows = db.groups.map(groupRow);

    if (query.active === "true") rows = rows.filter((g) => g.is_active);
    if (query.active === "false") rows = rows.filter((g) => !g.is_active);
    if (query.q) {
      const needle = query.q.trim().toLowerCase();
      rows = rows.filter(
        (g) => g.invite_code.toLowerCase().includes(needle) || matches(g.owner_name, needle)
      );
    }

    rows.sort((a, b) => b.created_at.localeCompare(a.created_at));
    return page(rows, Number(query.limit ?? 50), Number(query.offset ?? 0));
  },

  "GET /groups/{id}": (params) => {
    const group = db.groups.find((g) => g.id === params.id);
    if (!group) throw httpError(404, "No group with that id.");

    return {
      ...groupRow(group),
      members: db.groupMembers
        .filter((m) => m.group_id === group.id)
        .map((membership) => {
          const user = db.users.find((u) => u.id === membership.user_id);
          return {
            user_id: membership.user_id,
            full_name: user?.full_name ?? "(deleted)",
            phone: user?.phone ?? null,
            is_owner: membership.user_id === group.owner_id,
            joined_at: membership.created_at,
          };
        })
        .sort((a, b) => Number(b.is_owner) - Number(a.is_owner)),
    };
  },

  "GET /content/stats": () => contentStats(),

  "GET /content/materials": (_params, _body, context) => {
    const { query } = context;
    let rows = db.materials;

    if (query.extraction_status) {
      rows = rows.filter((m) => m.extraction_status === query.extraction_status);
    }
    if (query.kind) rows = rows.filter((m) => m.kind === query.kind);
    if (query.user_id) rows = rows.filter((m) => m.user_id === query.user_id);

    rows = [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at));
    return page(rows, Number(query.limit ?? 50), Number(query.offset ?? 0));
  },

  "GET /ops/health": () => {
    const content = contentStats();
    const integrations = {
      kora: true,
      supabase_storage: true,
      sms: true,
      google_sign_in: false,
    };

    const warnings = [];
    for (const [name, configured] of Object.entries(integrations)) {
      if (!configured) warnings.push(`${name} has no credentials in this environment.`);
    }
    if (content.extraction_stalled) {
      warnings.push(
        `${content.extraction_stalled} materials have been waiting over an hour for extraction — the worker may be down.`
      );
    }

    return {
      environment: "mock",
      database_ok: true,
      database_latency_ms: 3.4,
      integrations,
      counts: {
        users: liveUsers().length,
        devices: db.devices.length,
        subscriptions: db.users.filter((u) => u._sub).length,
        plan_groups: db.groups.length,
        payments: db.payments.length,
        materials: db.materials.length,
        material_chunks: content.material_chunks,
        messages: content.messages,
        trial_grants: db.users.length,
        live_refresh_tokens: Math.round(db.devices.length * 0.82),
        otp_codes: 41,
      },
      warnings,
    };
  },

  "GET /ops/plans": () =>
    Object.values(PLANS).map((plan) => ({
      id: plan.id,
      name: plan.name,
      price_ksh: plan.price_ksh,
      price_per_seat_ksh: Math.floor(plan.price_ksh / Math.max(1, plan.seats)),
      duration_days: plan.duration_days,
      seats: plan.seats,
      limits: PLAN_LIMITS[plan.id] ?? PLAN_LIMITS.expired,
    })),

  "GET /audit": (_params, _body, context) => {
    const { query } = context;
    let rows = db.auditLog;

    if (query.action) rows = rows.filter((row) => row.action === query.action);
    if (query.admin_id) rows = rows.filter((row) => row.admin_id === query.admin_id);
    if (query.target_id) rows = rows.filter((row) => row.target_id === query.target_id);

    return page(rows, Number(query.limit ?? 100), Number(query.offset ?? 0));
  },

  "GET /audit/actions": () => {
    const counts = new Map();
    for (const row of db.auditLog) counts.set(row.action, (counts.get(row.action) ?? 0) + 1);
    return [...counts.entries()]
      .map(([action, count]) => ({ action, count }))
      .sort((a, b) => b.count - a.count);
  },

  "GET /admins": () => db.admins,

  "POST /admins": (_params, body, context) => {
    if (db.admins.some((a) => a.email.toLowerCase() === body.email.toLowerCase())) {
      throw httpError(409, "An admin with that email already exists.");
    }
    const admin = {
      id: crypto.randomUUID(),
      email: body.email.toLowerCase(),
      full_name: body.full_name ?? "",
      role: body.role ?? "support",
      is_active: true,
      last_login_at: null,
      created_at: now().toISOString(),
    };
    db.admins.push(admin);

    logAction(context, {
      action: "admin.created",
      target_type: "admin",
      target_id: admin.id,
      summary: `Created ${admin.role} ${admin.email}`,
    });

    return admin;
  },

  "PATCH /admins/{id}": (params, body, context) => {
    const admin = db.admins.find((a) => a.id === params.id);
    if (!admin) throw httpError(404, "No admin with that id.");

    const losingOwner =
      admin.role === "owner" && (body.role !== undefined ? body.role !== "owner" : body.is_active === false);
    if (losingOwner) {
      const remaining = db.admins.filter(
        (a) => a.role === "owner" && a.is_active && a.id !== admin.id
      ).length;
      if (remaining === 0) {
        throw httpError(400, "That is the last active owner. Promote someone else first.");
      }
    }

    const { password, ...rest } = body;
    Object.assign(admin, rest);

    logAction(context, {
      action: "admin.updated",
      target_type: "admin",
      target_id: admin.id,
      summary: `Updated ${admin.email}: ${Object.keys(body).join(", ")}`,
    });

    return admin;
  },

  "DELETE /admins/{id}": (params, _body, context) => {
    const admin = db.admins.find((a) => a.id === params.id);
    if (!admin) throw httpError(404, "No admin with that id.");
    if (admin.id === currentAdmin(context).id) {
      throw httpError(400, "You cannot remove your own access.");
    }
    if (admin.role === "owner") {
      const remaining = db.admins.filter(
        (a) => a.role === "owner" && a.is_active && a.id !== admin.id
      ).length;
      if (remaining === 0) throw httpError(400, "That is the last active owner.");
    }

    admin.is_active = false;

    logAction(context, {
      action: "admin.removed",
      target_type: "admin",
      target_id: admin.id,
      summary: `Removed ${admin.email} — ${context.query.reason}`,
    });

    return { ok: true, message: `${admin.email} can no longer sign in.` };
  },

  "GET /admins/{id}/sessions": (params) => {
    const admin = db.admins.find((a) => a.id === params.id);
    if (!admin || !admin.last_login_at) return [];
    return [
      {
        id: crypto.randomUUID(),
        created_at: admin.last_login_at,
        expires_at: new Date(new Date(admin.last_login_at).getTime() + 7 * DAY_MS).toISOString(),
        ip: "41.90.64.12",
        user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0.0.0",
      },
    ];
  },
};

// --- Plumbing -----------------------------------------------------------------

function httpError(status, message) {
  const error = new Error(message);
  error.status = status;
  error.message = message;
  return error;
}

function requireUser(id) {
  const user = db.users.find((u) => u.id === id);
  if (!user) throw httpError(404, "No account with that id.");
  return user;
}

function currentAdmin(context) {
  const id = String(context.token ?? "").split(".")[1];
  return db.admins.find((a) => a.id === id) ?? db.admins[0];
}

/** Writes to the same log the audit page reads, so an action shows up there. */
function logAction(context, entry) {
  const admin = currentAdmin(context);
  db.auditLog.unshift({
    id: crypto.randomUUID(),
    admin_id: admin.id,
    admin_email: admin.email,
    meta: null,
    ip: "41.90.64.12",
    created_at: now().toISOString(),
    ...entry,
  });
}

/** `/users/abc/usage` → matches `GET /users/{id}/usage`, yielding `{ id: "abc" }`. */
function resolve(method, path) {
  const wanted = path.split("/").filter(Boolean);

  for (const key of Object.keys(routes)) {
    const [routeMethod, routePath] = key.split(" ");
    if (routeMethod !== method) continue;

    const parts = routePath.split("/").filter(Boolean);
    if (parts.length !== wanted.length) continue;

    const params = {};
    let matched = true;
    for (let i = 0; i < parts.length; i += 1) {
      if (parts[i].startsWith("{")) {
        params[parts[i].slice(1, -1)] = decodeURIComponent(wanted[i]);
      } else if (parts[i] !== wanted[i]) {
        matched = false;
        break;
      }
    }

    if (matched) return { handler: routes[key], params };
  }

  return null;
}

/**
 * The entry point `lib/api.js` calls instead of `fetch`.
 *
 * The small artificial delay is deliberate. Instant responses hide every
 * loading state, and a console designed without them falls apart the first
 * time it runs against a real database over a real connection.
 */
export async function mockRequest({ method, path, query = {}, body = null, token = null }) {
  await new Promise((resolve_) => setTimeout(resolve_, 120 + Math.random() * 180));

  const match = resolve(method, path);
  if (!match) throw httpError(404, `No mock route for ${method} ${path}`);

  return match.handler(match.params, body, { query, token });
}
