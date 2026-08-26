/*
 * Exercises every mock route and checks the numbers agree with each other.
 *
 *     node scripts/check-mocks.mjs
 *
 * Two jobs. The first is a smoke test — a route that throws is a page that
 * renders a stack trace. The second matters more: it re-derives the headline
 * figures from the raw fixture and asserts the aggregate endpoints match. A
 * dashboard designed against numbers that cannot coexist is a dashboard that
 * looks fine and teaches you nothing.
 */

import { mockRequest } from "../src/lib/mock/server.js";
import { PLANS, db } from "../src/lib/mock/dataset.js";

let failures = 0;
let checks = 0;

function check(label, condition, detail = "") {
  checks += 1;
  if (condition) return;
  failures += 1;
  console.error(`  FAIL  ${label}${detail ? ` — ${detail}` : ""}`);
}

async function call(method, path, query = {}, body = null) {
  return mockRequest({ method, path, query, body, token: `mock.${db.admins[0].id}` });
}

const sampleUser = db.users.find((u) => !u.deleted_at && u._sub);
const samplePayment = db.payments[0];
const sampleGroup = db.groups[0];
const sampleMaterial = db.materials[0];
const sampleAdmin = db.admins[0];

// --- Every route responds ------------------------------------------------------

const ROUTES = [
  ["POST", "/auth/login", {}, { email: sampleAdmin.email, password: "anything" }],
  ["GET", "/auth/me"],
  ["GET", "/overview"],
  ["GET", "/overview/timeseries", { metric: "signups", days: 30 }],
  ["GET", "/overview/timeseries", { metric: "revenue", days: 90 }],
  ["GET", "/overview/institutions", { limit: 8 }],
  ["GET", "/users", { limit: 25, offset: 0 }],
  ["GET", "/users", { q: "wan", status: "paying", limit: 10 }],
  ["GET", `/users/${sampleUser.id}`],
  ["GET", `/users/${sampleUser.id}/usage`],
  ["GET", "/subscriptions", { limit: 25 }],
  ["GET", "/subscriptions", { verified: "false" }],
  ["GET", "/subscriptions/stats"],
  ["GET", "/revenue/summary"],
  ["GET", "/revenue/by-plan"],
  ["GET", "/revenue/timeseries", { metric: "revenue", days: 30 }],
  ["GET", "/revenue/top-customers", { limit: 10 }],
  ["GET", "/payments", { limit: 25 }],
  ["GET", "/payments", { status: "pending" }],
  ["GET", `/payments/${samplePayment.id}`],
  ["GET", "/groups", { limit: 25 }],
  ["GET", `/groups/${sampleGroup.id}`],
  ["GET", "/content/stats"],
  ["GET", "/content/materials", { limit: 25 }],
  ["GET", "/content/materials", { extraction_status: "failed" }],
  ["GET", "/ops/health"],
  ["GET", "/ops/plans"],
  ["GET", "/audit", { limit: 30 }],
  ["GET", "/audit/actions"],
  ["GET", "/admins"],
  ["GET", `/admins/${sampleAdmin.id}/sessions`],
];

console.log("Routes");
for (const [method, path, query, body] of ROUTES) {
  try {
    const result = await call(method, path, query, body);
    check(`${method} ${path}`, result !== undefined && result !== null);
  } catch (error) {
    check(`${method} ${path}`, false, error.message);
  }
}

// --- The figures agree ---------------------------------------------------------

console.log("\nConsistency");

const overview = await call("GET", "/overview");
const revenue = await call("GET", "/revenue/summary");
const plans = await call("GET", "/revenue/by-plan");
const stats = await call("GET", "/subscriptions/stats");
const usersPage = await call("GET", "/users", { limit: 1 });

// Gross revenue must equal the sum of successful payments, exactly.
const grossFromRows = db.payments
  .filter((p) => p.status === "success")
  .reduce((total, p) => total + p.amount_kes, 0);
check(
  "gross revenue matches the payment rows",
  revenue.gross_ksh === grossFromRows,
  `${revenue.gross_ksh} vs ${grossFromRows}`
);

// The dashboard and the revenue page must not disagree.
check(
  "overview and revenue page report the same MRR",
  overview.revenue.mrr_ksh === revenue.mrr_ksh
);
check(
  "revenue summary MRR equals the sum of per-plan MRR",
  revenue.mrr_ksh === plans.reduce((total, row) => total + row.mrr_ksh, 0)
);
check(
  "subscription stats and revenue page agree on MRR",
  stats.mrr_ksh === revenue.mrr_ksh
);
check(
  "paying customers match between overview and subscriptions",
  overview.revenue.paying_customers === stats.total_paying
);

// The users table total must match the dashboard's user count.
check(
  "users table total matches the overview count",
  usersPage.total === overview.users.total,
  `${usersPage.total} vs ${overview.users.total}`
);

// Friends is the trap: one payment, up to five seats. MRR must count groups.
const friends = plans.find((row) => row.tier === "friends");
const liveGroups = db.groups.filter((g) => new Date(g.expires_at) > new Date()).length;
check(
  "Friends MRR counts groups, not seats",
  friends.mrr_ksh === liveGroups * PLANS.friends.price_ksh,
  `${friends.mrr_ksh} for ${liveGroups} live groups; seats would give ${friends.active * PLANS.friends.price_ksh}`
);
check(
  "Friends seats outnumber Friends groups (so the trap is actually exercised)",
  friends.active > liveGroups,
  `${friends.active} seats across ${liveGroups} groups`
);

// A trial is active and is not a customer.
const trial = plans.find((row) => row.tier === "trial");
check("trial contributes no MRR", trial.mrr_ksh === 0);
check("trial counts nobody as paying", trial.paying === 0);
check("there are trials to count", trial.active > 0);

// Timeseries must be dense — every day present, in order.
for (const days of [7, 30, 90]) {
  const series = await call("GET", "/overview/timeseries", { metric: "revenue", days });
  check(`${days}-day series has ${days} points`, series.points.length === days);
  const sorted = [...series.points].sort((a, b) => a.day.localeCompare(b.day));
  check(`${days}-day series is in date order`, JSON.stringify(sorted) === JSON.stringify(series.points));
  check(
    `${days}-day series total equals the sum of its points`,
    series.total === series.points.reduce((sum, p) => sum + p.value, 0)
  );
}

// The 30-day series must match the 30-day figure on the summary. They come from
// different code paths and are the same claim.
const series30 = await call("GET", "/revenue/timeseries", { metric: "revenue", days: 30 });
check(
  "30-day revenue series roughly matches last_30d_ksh",
  Math.abs(series30.total - revenue.last_30d_ksh) / Math.max(1, revenue.last_30d_ksh) < 0.08,
  `series ${series30.total} vs summary ${revenue.last_30d_ksh} (day-boundary vs rolling window)`
);

// The attention banner must be reachable — the reconcile and stalled-extraction
// screens only exist because these states do.
check("the dashboard has something to flag", overview.attention.length > 0);
check(
  "unverified paid subscriptions exist to design against",
  overview.attention.some((item) => item.code === "unverified_paid_subscriptions")
);

const pending = await call("GET", "/payments", { status: "pending" });
check("there are pending payments to reconcile", pending.total > 0);

const failedMaterials = await call("GET", "/content/materials", { extraction_status: "failed" });
check("there are failed extractions to look at", failedMaterials.total > 0);

// One user detail response must hang together.
const detail = await call("GET", `/users/${sampleUser.id}`);
check("user detail has a subscription block", detail.subscription !== undefined);
check("user detail resolves an effective tier", Boolean(detail.effective_tier));
check("user detail carries limits", typeof detail.limits.daily_ai_queries === "number");
check(
  "user detail never leaks simulation state",
  !Object.keys(detail).some((key) => key.startsWith("_")),
  Object.keys(detail).filter((key) => key.startsWith("_")).join(", ")
);

const detailPaid = detail.payments
  .filter((p) => p.status === "success")
  .reduce((total, p) => total + p.amount_kes, 0);
check(
  "lifetime spend matches the charges listed",
  detail.total_paid_ksh >= detailPaid,
  `${detail.total_paid_ksh} vs ${detailPaid} in the 50 shown`
);

// Pagination must not lose or duplicate rows.
const first = await call("GET", "/users", { limit: 10, offset: 0 });
const second = await call("GET", "/users", { limit: 10, offset: 10 });
const overlap = first.items.filter((row) => second.items.some((other) => other.id === row.id));
check("pages do not overlap", overlap.length === 0);
check("page totals agree", first.total === second.total);

// A group must never seat more people than it sold.
const groups = await call("GET", "/groups", { limit: 200 });
check(
  "no group is oversubscribed",
  groups.items.every((group) => group.seats_taken <= group.seats),
  groups.items.filter((g) => g.seats_taken > g.seats).map((g) => g.invite_code).join(", ")
);

// --- Scale ---------------------------------------------------------------------

console.log("\nFixture shape");
console.log(`  users        ${db.users.length}`);
console.log(`  payments     ${db.payments.length}`);
console.log(`  groups       ${db.groups.length}`);
console.log(`  materials    ${db.materials.length}`);
console.log(`  audit        ${db.auditLog.length}`);
console.log(`  gross        KES ${grossFromRows.toLocaleString()}`);
console.log(`  MRR          KES ${revenue.mrr_ksh.toLocaleString()}`);
console.log(`  paying       ${revenue.paying_customers}`);
console.log(`  conversion   ${overview.funnel.trial_conversion_pct}%`);

check("enough users to fill a table", db.users.length > 300);
check("enough payments for a chart", db.payments.length > 200);
check("conversion is plausible", overview.funnel.trial_conversion_pct > 5 && overview.funnel.trial_conversion_pct < 60);

console.log(`\n${checks - failures}/${checks} checks passed`);
process.exit(failures ? 1 : 0);
