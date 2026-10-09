// Indian number formatting for the landing page (lakh and crore grouping, IST clock).

const RUPEE = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const COUNT = new Intl.NumberFormat("en-IN");

/** 280000 paise -> "₹2,800.00". */
export function inr(paise: number): string {
  return `₹${RUPEE.format(paise / 100)}`;
}

/** Price without the rupee sign, for dense rows: 280000 -> "2,800.00". */
export function price(paise: number): string {
  return RUPEE.format(paise / 100);
}

/** Whole numbers with Indian grouping: 1234567 -> "12,34,567". */
export function count(n: number): string {
  return COUNT.format(n);
}

/** Basis points as a signed percentage: -388 -> "-3.88%". */
export function bpsPct(bps: number): string {
  const v = (bps / 100).toFixed(2);
  return `${bps > 0 ? "+" : ""}${v}%`;
}

const IST_TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** "14:05:09" in India Standard Time. */
export function istTime(ms: number): string {
  return IST_TIME.format(ms);
}
