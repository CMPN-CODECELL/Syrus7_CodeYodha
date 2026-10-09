// Display helpers for broker accounts. The 021 account id is the trader's UCC: it is always masked on screen.

import type { BrokerKind, CanonicalOrder } from "./types";

const UCC_RE = /^HACK\d+$/i;

/** "HACK1234" becomes "HACK••34". Short ids keep only their last two characters. */
export function maskUcc(id: string): string {
  const v = id.trim();
  if (v.length <= 4) return "•".repeat(Math.max(2, v.length));
  const head = v.length > 6 ? v.slice(0, 4) : "";
  const tail = v.slice(-2);
  return `${head}${"•".repeat(Math.max(2, v.length - head.length - 2))}${tail}`;
}

/** An account id as it may be shown: masked for 021 (or anything shaped like a UCC), as is for paper accounts. */
export function displayAccountId(id: string | null | undefined, kind?: BrokerKind | null): string {
  if (!id) return "-";
  return kind === "021" || UCC_RE.test(id) ? maskUcc(id) : id;
}

/** Masks every UCC-shaped token inside a longer text, such as "HACK1234 (021 Sandbox)". */
export function maskUccsIn(text: string): string {
  return text.replace(/\bHACK\d+\b/gi, (m) => maskUcc(m));
}

export function brokerName(kind: BrokerKind | null | undefined): string {
  return kind === "021" ? "021 Sandbox" : "Paper account";
}

/** Broker as a system, for status rows. */
export function brokerSystemName(kind: BrokerKind | null | undefined): string {
  return kind === "021" ? "021 Sandbox" : "Paper broker";
}

function rupeesText(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "";
  const r = paise / 100;
  return Number.isInteger(r) ? String(r) : r.toFixed(2);
}

const PRODUCT_WORD: Record<string, string> = { MIS: "intraday", INTRADAY: "intraday", CNC: "delivery", NRML: "NRML" };

/**
 * The trader's instruction for drafting the same order again, built from the canonical order, so a step that was
 * not placed can be re-proposed on its own. Drafting is never sending: the new proposal needs a fresh countersignature.
 */
export function redraftText(c: CanonicalOrder, fallback: string): string {
  if (c.action !== "PLACE") return fallback;
  const verb = c.side === "BUY" ? "Buy" : "Sell";
  const parts = [`${verb} ${c.quantity} ${c.instrument.symbol}`];
  if (c.order_type === "MARKET") parts.push("at market");
  else if (c.order_type === "LIMIT") parts.push(`at ${rupeesText(c.limit_price_paise)} limit`);
  else if (c.order_type === "SL") parts.push(`with a stop-loss trigger at ${rupeesText(c.trigger_price_paise)}, limit ${rupeesText(c.limit_price_paise)}`);
  else if (c.order_type === "SL-M") parts.push(`with a stop-loss trigger at ${rupeesText(c.trigger_price_paise)}`);
  const product = PRODUCT_WORD[c.product];
  if (product) parts.push(product);
  if (c.validity === "IOC") parts.push("IOC");
  return parts.join(" ");
}
