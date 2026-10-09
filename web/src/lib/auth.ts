// Client-side auth helpers: safe redirects, onboarding state, PIN and password hints.
// The server is always the authority; these only give instant feedback.

import type { Me } from "./types";

export const DEFAULT_AFTER_SIGNIN = "/copilot";

const AUTH_PATHS = ["/signin", "/signup"];

/**
 * Only same-origin relative paths are allowed as ?next= targets (no open redirect).
 * Anything else, or a path back into sign-in or sign-up, falls back to the Copilot.
 */
export function safeNext(raw: string | null | undefined, fallback = DEFAULT_AFTER_SIGNIN): string {
  if (!raw || typeof raw !== "string") return fallback;
  const v = raw.trim();
  // Must be a rooted path; "//host" and "/\\host" are protocol-relative in browsers.
  if (!v.startsWith("/") || v.startsWith("//") || v.includes("\\")) return fallback;
  for (let i = 0; i < v.length; i++) if (v.charCodeAt(i) < 0x20) return fallback;
  let url: URL;
  try {
    url = new URL(v, "http://countersign.invalid");
  } catch {
    return fallback;
  }
  if (url.origin !== "http://countersign.invalid") return fallback;
  if (AUTH_PATHS.some((p) => url.pathname === p || url.pathname.startsWith(`${p}/`))) return fallback;
  if (url.pathname.startsWith("/api")) return fallback;
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Builds "/signin?next=..." style links that carry the destination along. */
export function withNext(path: string, next: string | null | undefined): string {
  const n = next ? safeNext(next, "") : "";
  return n && n !== DEFAULT_AFTER_SIGNIN ? `${path}?next=${encodeURIComponent(n)}` : path;
}

/** An older backend sends no onboarding field: treat that as complete so it never traps a session. */
export function onboardingComplete(me: Me): boolean {
  return me.onboarding ? me.onboarding.complete !== false : true;
}

/** Where to go right after a successful sign-in or sign-up. */
export function afterSignIn(me: Me, next: string): string {
  return onboardingComplete(me) ? next : `/onboarding?next=${encodeURIComponent(next)}`;
}

export function accountId(me: Me): string {
  return me.account?.id ?? me.account_id;
}

export function activePasskeys(me: Me): number {
  return me.devices.filter((d) => !d.revoked && !d.is_virtual).length;
}

/** Mirrors the server's WEAK_PIN rule: all one digit, or a straight ascending or descending run. */
export function weakPinReason(pin: string): string | null {
  if (!/^\d{4}$/.test(pin)) return null;
  const d = [...pin].map(Number);
  if (d.every((x) => x === d[0])) return "All four digits are the same. Pick something harder to guess.";
  const steps = d.slice(1).map((x, i) => x - d[i]);
  if (steps.every((s) => s === 1)) return "That is an ascending run like 1234. Pick something harder to guess.";
  if (steps.every((s) => s === -1)) return "That is a descending run like 4321. Pick something harder to guess.";
  return null;
}

export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 128;

export interface Strength {
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
  hint: string;
}

/** A rough, honest strength hint (length plus character variety). Not a guarantee. */
export function passwordStrength(pw: string, email = ""): Strength {
  if (!pw) return { score: 0, label: "", hint: `At least ${PASSWORD_MIN} characters.` };
  if (pw.length < PASSWORD_MIN) {
    return { score: 1, label: "Too short", hint: `${PASSWORD_MIN - pw.length} more character${PASSWORD_MIN - pw.length === 1 ? "" : "s"} needed.` };
  }
  if (email && pw.trim().toLowerCase() === email.trim().toLowerCase()) {
    return { score: 1, label: "Same as email", hint: "Your password must not be your email address." };
  }
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(pw)).length;
  const repetitive = /^(.)\1+$/.test(pw) || /^(?:0123|1234|abcd|qwer|pass)/i.test(pw);
  let score = 1 + (pw.length >= 12 ? 1 : 0) + (classes >= 3 ? 1 : 0) + (pw.length >= 16 || (pw.length >= 12 && classes === 4) ? 1 : 0);
  if (repetitive) score = Math.min(score, 1);
  const s = Math.min(4, Math.max(1, score)) as 1 | 2 | 3 | 4;
  const table: Record<1 | 2 | 3 | 4, Strength> = {
    1: { score: 1, label: "Weak", hint: "Longer is stronger. Try a short phrase of unrelated words." },
    2: { score: 2, label: "Fair", hint: "Add length or mix in numbers and symbols." },
    3: { score: 3, label: "Good", hint: "A password manager can make it even longer." },
    4: { score: 4, label: "Strong", hint: "Nice. Keep it unique to Countersign." },
  };
  return table[s];
}
