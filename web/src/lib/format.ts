// Formatting helpers. Money arrives as integer paise and is never held as floating rupees
// longer than needed for display.

import type { Bps, Paise } from "./types";

const RUPEE = "₹";

const groupFmt2 = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const groupFmt0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const qtyFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/** Group an integer string with Indian digit grouping (12,34,56,789). Exact for any size. */
function groupIndian(intDigits: string): string {
  if (intDigits.length <= 3) return intDigits;
  const last3 = intDigits.slice(-3);
  let rest = intDigits.slice(0, -3);
  const parts: string[] = [];
  while (rest.length > 2) {
    parts.unshift(rest.slice(-2));
    rest = rest.slice(0, -2);
  }
  if (rest) parts.unshift(rest);
  return `${parts.join(",")},${last3}`;
}

/**
 * Exact INR formatting from integer paise, no float rounding: 1234567850 -> "₹1,23,45,678.50".
 * Options: decimals (default true), sign ("auto" | "always").
 */
export function inr(
  paise: Paise | null | undefined,
  opts: { decimals?: boolean; sign?: "auto" | "always"; symbol?: boolean } = {},
): string {
  if (paise === null || paise === undefined || Number.isNaN(paise)) return "-";
  const { decimals = true, sign = "auto", symbol = true } = opts;
  const negative = paise < 0;
  const abs = Math.abs(Math.round(paise));
  const rupees = Math.floor(abs / 100);
  const fraction = abs % 100;
  let body = groupIndian(String(rupees));
  if (decimals) body += `.${String(fraction).padStart(2, "0")}`;
  const prefix = symbol ? RUPEE : "";
  if (negative) return `-${prefix}${body}`;
  if (sign === "always" && paise > 0) return `+${prefix}${body}`;
  return `${prefix}${body}`;
}

/** Signed P&L, always shows + or - (zero shows no sign). */
export function pnl(paise: Paise | null | undefined, decimals = true): string {
  return inr(paise, { sign: "always", decimals });
}

/**
 * Compact lakh / crore: ₹1.23 L, ₹4.56 Cr, ₹12,345 Cr. Never "KCr".
 * Below one lakh shows the full rupee amount without decimals.
 */
export function inrCompact(
  paise: Paise | null | undefined,
  opts: { sign?: "auto" | "always" } = {},
): string {
  if (paise === null || paise === undefined || Number.isNaN(paise)) return "-";
  const rupees = paise / 100;
  const abs = Math.abs(rupees);
  const neg = rupees < 0;
  const sign = neg ? "-" : opts.sign === "always" && rupees > 0 ? "+" : "";
  let body: string;
  if (abs >= 1e7) {
    const cr = abs / 1e7;
    body = cr >= 1000 ? `${groupFmt0.format(Math.round(cr))} Cr` : `${trim2(cr)} Cr`;
  } else if (abs >= 1e5) {
    body = `${trim2(abs / 1e5)} L`;
  } else {
    body = groupFmt0.format(Math.round(abs));
  }
  return `${sign}${RUPEE}${body}`;
}

function trim2(n: number): string {
  return (Math.round(n * 100) / 100).toFixed(2);
}

/** Paise to rupees as a number (for charts and NumberFlow). */
export function rupees(paise: Paise): number {
  return paise / 100;
}

/** Price without currency symbol, 2 decimals, en-IN grouping. */
export function price(paise: Paise | null | undefined): string {
  if (paise === null || paise === undefined) return "-";
  return inr(paise, { symbol: false });
}

/** Basis points to percent string. 523 -> "5.23%". */
export function pct(bps: Bps | null | undefined, opts: { sign?: boolean; digits?: number } = {}): string {
  if (bps === null || bps === undefined || Number.isNaN(bps)) return "-";
  const { sign = false, digits = 2 } = opts;
  const v = bps / 100;
  const s = v.toFixed(digits);
  if (sign && v > 0) return `+${s}%`;
  return `${s}%`;
}

export function qty(n: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (n === null || n === undefined) return "-";
  const s = qtyFmt.format(Math.abs(n));
  if (n < 0) return `-${s}`;
  if (opts.sign && n > 0) return `+${s}`;
  return s;
}

export function num(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined) return "-";
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: digits }).format(n);
}

/** Compact counts for volume and OI: 1.2 L, 3.4 Cr, 45,600. */
export function compactCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return "-";
  const abs = Math.abs(n);
  if (abs >= 1e7) return `${trim2(n / 1e7)} Cr`;
  if (abs >= 1e5) return `${trim2(n / 1e5)} L`;
  return groupFmt0.format(n);
}

export { groupFmt2 };

/** Direction of a value for coloring. */
export function tone(n: number | null | undefined): "up" | "down" | "flat" {
  if (!n) return "flat";
  return n > 0 ? "up" : "down";
}

export function toneClass(n: number | null | undefined): string {
  const t = tone(n);
  return t === "up" ? "text-profit" : t === "down" ? "text-loss" : "text-muted-foreground";
}

// Time

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});
const dateTimeFmt = new Intl.DateTimeFormat("en-IN", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});
const dateFmt = new Intl.DateTimeFormat("en-IN", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: "Asia/Kolkata",
});

export function time(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return timeFmt.format(d);
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return dateTimeFmt.format(d);
}

export function date(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00+05:30` : iso);
  if (Number.isNaN(d.getTime())) return "-";
  return dateFmt.format(d);
}

/** "3 s ago", "4 min ago", "2 h ago". `now` passed in so callers control re-render cadence. */
export function ago(iso: string | null | undefined, now: number): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "-";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

/** Remaining time "in 42 s" / "in 3 d" for expiries. */
export function until(iso: string | null | undefined, now: number): string {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  const s = Math.round((t - now) / 1000);
  if (s <= 0) return "expired";
  if (s < 60) return `in ${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `in ${m} min`;
  const h = Math.round(m / 60);
  if (h < 48) return `in ${h} h`;
  return `in ${Math.round(h / 24)} d`;
}

export function ms(n: number | null | undefined): string {
  if (n === null || n === undefined) return "-";
  if (n < 1000) return `${Math.round(n)} ms`;
  return `${(n / 1000).toFixed(1)} s`;
}

/** Short hash for display: first n hex chars. */
export function shortHash(h: string | null | undefined, n = 10): string {
  if (!h) return "-";
  return h.slice(0, n);
}

/** Group a long hex hash into 4-char blocks for legibility. */
export function chunkHash(h: string, size = 4): string {
  return h.match(new RegExp(`.{1,${size}}`, "g"))?.join(" ") ?? h;
}

/** Human label for SCREAMING_SNAKE enums. */
export function humanize(s: string): string {
  return s
    .toLowerCase()
    .split("_")
    .map((w, i) => (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export function optionLabel(strikePaise: Paise | null | undefined, type: "CE" | "PE" | null | undefined): string {
  if (!strikePaise || !type) return "";
  return `${groupFmt0.format(strikePaise / 100)} ${type}`;
}
