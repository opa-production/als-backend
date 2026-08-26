/*
 * The plan catalogue, as the server defines it.
 *
 * A mirror of `app/services/plans.py`. Three copies of these numbers now exist
 * — here, in the API, and in the mobile app's `src/theme/plans.js` — and the
 * API is the authority. When a price changes there it changes here in the same
 * commit, or the console starts quoting figures nobody is charged.
 *
 * The console uses this only for **labels and explanation**: naming a tier,
 * showing what a Friends seat costs per head. Every number that is actually
 * reported comes from the API. `/admin/ops/plans` returns the server's live
 * copy, which is the screen to open when the two are suspected of disagreeing.
 */

export const PLANS = {
  expired: { id: "expired", name: "Expired", price_ksh: 0, duration_days: 0, seats: 1 },
  trial: { id: "trial", name: "14-Day Free Trial", price_ksh: 0, duration_days: 14, seats: 1 },
  standard: { id: "standard", name: "Focus", price_ksh: 150, duration_days: 30, seats: 1 },
  pro: { id: "pro", name: "Synapse", price_ksh: 350, duration_days: 30, seats: 1 },
  friends: { id: "friends", name: "Friends", price_ksh: 1250, duration_days: 30, seats: 5 },
};

/** The tiers a student can actually buy, in the order they are shown. */
export const SELLABLE = ["standard", "pro", "friends"];

export const PLAN_LIMITS = {
  expired: {
    max_course_units: 0,
    total_pdf_pages_pool: 0,
    max_single_file_size_mb: 0,
    max_single_file_pages: 0,
    daily_ai_queries: 0,
    quiz_count: 0,
    quiz_interval: "lifetime",
    quiz_max_questions: 0,
    timetable_mode: "manual",
    source_citations: "basic",
    allow_ocr_scans: false,
    monthly_ocr_page_limit: 0,
  },
  trial: {
    max_course_units: 2,
    total_pdf_pages_pool: 100,
    max_single_file_size_mb: 10,
    max_single_file_pages: 30,
    daily_ai_queries: 15,
    quiz_count: 2,
    quiz_interval: "lifetime",
    quiz_max_questions: 5,
    timetable_mode: "manual",
    source_citations: "basic",
    allow_ocr_scans: false,
    monthly_ocr_page_limit: 0,
  },
  standard: {
    max_course_units: 4,
    total_pdf_pages_pool: 400,
    max_single_file_size_mb: 25,
    max_single_file_pages: 100,
    daily_ai_queries: 40,
    quiz_count: 5,
    quiz_interval: "weekly",
    quiz_max_questions: 10,
    timetable_mode: "alerts",
    source_citations: "exact_page",
    allow_ocr_scans: false,
    monthly_ocr_page_limit: 0,
  },
  pro: {
    max_course_units: 10,
    total_pdf_pages_pool: 1500,
    max_single_file_size_mb: 50,
    max_single_file_pages: 300,
    daily_ai_queries: 120,
    quiz_count: -1,
    quiz_interval: "unlimited",
    quiz_max_questions: 20,
    timetable_mode: "ai_sync",
    source_citations: "deep_summary",
    allow_ocr_scans: true,
    monthly_ocr_page_limit: 30,
  },
};

// Friends is Synapse's limits at a group price — the same object, not a copy,
// so a number changed on Pro cannot drift away from Friends.
PLAN_LIMITS.friends = PLAN_LIMITS.pro;

export function planName(tier) {
  return PLANS[tier]?.name ?? "Expired";
}
