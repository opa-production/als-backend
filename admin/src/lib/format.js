/*
 * Formatting.
 *
 * All of it lives here rather than inline, because the same number formatted
 * two different ways on two screens is how a reader stops trusting a console.
 */

const KES = new Intl.NumberFormat("en-KE", {
  style: "currency",
  currency: "KES",
  maximumFractionDigits: 0,
});

const PLAIN = new Intl.NumberFormat("en-KE");

/** `KES 1,250`. Whole shillings — the API stores and returns integers. */
export function money(amount) {
  return KES.format(amount ?? 0);
}

/**
 * `KES 1.2M` for headline figures.
 *
 * Compact only where space is tight and the exact digit does not change a
 * decision. A ledger row always gets the full number.
 */
export function moneyCompact(amount) {
  const value = amount ?? 0;
  if (Math.abs(value) >= 1_000_000) return `KES ${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 100_000) return `KES ${Math.round(value / 1000)}K`;
  return KES.format(value);
}

export function number(value) {
  return PLAIN.format(value ?? 0);
}

export function compact(value) {
  const n = value ?? 0;
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 10_000) return `${Math.round(n / 1000)}K`;
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return PLAIN.format(n);
}

export function percent(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)}%`;
}

export function bytes(value) {
  const n = value ?? 0;
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

const DATE = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
});

const DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function date(value) {
  if (!value) return "—";
  return DATE.format(new Date(value));
}

export function dateTime(value) {
  if (!value) return "—";
  return DATE_TIME.format(new Date(value));
}

/** `3 days ago` / `in 12 days`. */
export function relative(value) {
  if (!value) return "—";
  const diff = new Date(value).getTime() - Date.now();
  const days = Math.round(diff / 86_400_000);

  if (Math.abs(days) >= 1) {
    const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
    if (Math.abs(days) >= 60) return rtf.format(Math.round(days / 30), "month");
    return rtf.format(days, "day");
  }

  const hours = Math.round(diff / 3_600_000);
  if (Math.abs(hours) >= 1) {
    return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(hours, "hour");
  }

  const minutes = Math.round(diff / 60_000);
  return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(minutes, "minute");
}

/** `WK` from "Wanjiru Kamau" — the avatar fallback. */
export function initials(name) {
  if (!name) return "—";
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("");
}

/** `ai_queries` → `AI queries`, `mobile_money` → `Mobile money`. */
export function humanise(key) {
  if (!key) return "";
  const spaced = key.replace(/_/g, " ");
  const sentence = spaced.charAt(0).toUpperCase() + spaced.slice(1);
  return sentence
    .replace(/\bAi\b/g, "AI")
    .replace(/\bOcr\b/g, "OCR")
    .replace(/\bPdf\b/g, "PDF")
    .replace(/\bMrr\b/g, "MRR")
    .replace(/\bArpu\b/g, "ARPU");
}

/** `Mon 3 Feb` — the x-axis tick on a daily chart. */
export function axisDay(isoDay) {
  const d = new Date(`${isoDay}T00:00:00`);
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" }).format(d);
}

export function fullDay(isoDay) {
  const d = new Date(`${isoDay}T00:00:00`);
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
  }).format(d);
}

/** Which badge tone a payment status gets. Status colour is never alone — the
 *  badge always carries the word too. */
export function paymentTone(status) {
  return (
    { success: "good", pending: "warn", failed: "danger", abandoned: "neutral" }[status] ??
    "neutral"
  );
}

export function extractionTone(status) {
  return (
    { done: "good", running: "info", pending: "warn", failed: "danger", skipped: "neutral" }[
      status
    ] ?? "neutral"
  );
}

export function tierTone(tier) {
  return (
    { pro: "info", friends: "info", standard: "neutral", trial: "warn", expired: "neutral" }[
      tier
    ] ?? "neutral"
  );
}
