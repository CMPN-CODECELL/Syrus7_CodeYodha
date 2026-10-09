"use client";

import { useQuery } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { useEffect, useRef, useState, useSyncExternalStore, type RefObject } from "react";
import { useOptionalMe } from "@/lib/queries";

const noopSubscribe = () => () => {};

/** False during SSR and hydration, true afterwards. Avoids hydration mismatches for client-only UI. */
export function useIsClient(): boolean {
  return useSyncExternalStore(
    noopSubscribe,
    () => true,
    () => false,
  );
}

function subscribeMedia(query: string) {
  return (onChange: () => void) => {
    const mql = window.matchMedia(query);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  };
}

const REDUCED = "(prefers-reduced-motion: reduce)";
const subscribeReduced = subscribeMedia(REDUCED);

export function useReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReduced,
    () => window.matchMedia(REDUCED).matches,
    () => false,
  );
}

/** Dark unless the resolved theme is explicitly light (dark is the product default). */
export function useIsDark(): boolean {
  const { resolvedTheme } = useTheme();
  return resolvedTheme !== "light";
}

/**
 * Tracks whether an element is on screen. With `once`, it latches true after the first
 * intersection (used for reveals and count-ups); otherwise it follows visibility (used to
 * pause animations off-screen).
 */
export function useInView<T extends Element>(
  ref: RefObject<T | null>,
  { once = false, rootMargin = "0px", threshold = 0 }: { once?: boolean; rootMargin?: string; threshold?: number } = {},
): boolean {
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true);
          if (once) io.disconnect();
        } else if (!once) {
          setInView(false);
        }
      },
      { rootMargin, threshold },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [ref, once, rootMargin, threshold]);
  return inView;
}

const subscribeVisibility = (onChange: () => void) => {
  document.addEventListener("visibilitychange", onChange);
  return () => document.removeEventListener("visibilitychange", onChange);
};

/** True while the browser tab is visible. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => !document.hidden,
    () => true,
  );
}

/** True while the element is on screen and the tab is visible: the condition for running a demo loop. */
export function useLive<T extends Element>(ref: RefObject<T | null>): boolean {
  const inView = useInView(ref);
  const visible = usePageVisible();
  return inView && visible;
}

/**
 * Runs `tick` every `ms` while `active`. The callback is kept in a ref so the timer is not
 * recreated on every render.
 */
export function useInterval(tick: () => void, ms: number, active: boolean) {
  const saved = useRef(tick);
  useEffect(() => {
    saved.current = tick;
  });
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => saved.current(), ms);
    return () => window.clearInterval(id);
  }, [ms, active]);
}

// ---------------------------------------------------------------------------
// Session

/**
 * Signed-in state for the landing CTAs. Shares the app's ["me"] query, and skips it without a session cookie, so
 * signed-out visitors cause no 401. `pending` means not known yet: render neither the signed-in nor signed-out CTAs.
 */
export function useLandingSession() {
  const { me, pending } = useOptionalMe();
  const firstName = me?.user.display_name?.trim().split(/\s+/)[0] || me?.user.username || null;
  return { signedIn: !!me, firstName, pending };
}

/**
 * Destination of "Explore the Countersign Copilot": straight in when signed in (or not known yet: the app's route
 * guard sends a signed-out visitor to sign-in anyway), sign in first otherwise.
 */
export function exploreHref(signedIn: boolean, pending = false): string {
  return signedIn || pending ? "/copilot" : "/signin?next=/copilot";
}

// ---------------------------------------------------------------------------
// Public status (GET /api/public/status, no auth)

export interface PublicStatus {
  all_operational: boolean;
  systems: { api: boolean; broker: boolean; feed: boolean; executor: boolean; monitor: boolean };
  unapproved_executions: number;
  attacks_attempted: number;
  attacks_blocked: number;
  last_attack_run_at: string | null;
  audit_events: number;
  market: { mode: "replay" | "live"; nifty: { ltp_paise: number; change_bps: number } | null };
  generated_at: string;
}

export interface LandingStatus extends PublicStatus {
  /** NIFTY last prices (paise) collected by this page from successive polls, oldest first. */
  niftyHistory: number[];
}

function isPublicStatus(v: unknown): v is PublicStatus {
  if (!v || typeof v !== "object") return false;
  const s = v as Partial<PublicStatus>;
  return (
    typeof s.all_operational === "boolean" &&
    !!s.systems &&
    typeof s.systems === "object" &&
    typeof s.unapproved_executions === "number" &&
    typeof s.attacks_attempted === "number" &&
    typeof s.attacks_blocked === "number" &&
    typeof s.audit_events === "number"
  );
}

// Samples survive re-renders and remounts for the lifetime of the page.
const niftySamples: number[] = [];

async function fetchPublicStatus(): Promise<LandingStatus> {
  const res = await fetch("/api/public/status", { cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`Status endpoint returned HTTP ${res.status}`);
  const data: unknown = await res.json();
  if (!isPublicStatus(data)) throw new Error("Status endpoint returned an unexpected shape");
  const ltp = data.market?.nifty?.ltp_paise;
  if (typeof ltp === "number" && Number.isFinite(ltp)) {
    niftySamples.push(ltp);
    if (niftySamples.length > 48) niftySamples.shift();
  }
  return { ...data, niftyHistory: niftySamples.slice() };
}

export type StatusState = "loading" | "ok" | "degraded" | "unavailable";

/**
 * Polls the public status every 5 s. Every consumer on the page shares one request.
 * `state` is "unavailable" whenever the latest poll failed, so stale numbers are never shown as live.
 */
export function usePublicStatus() {
  const q = useQuery({
    queryKey: ["public-status"],
    queryFn: fetchPublicStatus,
    // Every 5 s while it answers; back off to 15 s while it does not (for example before the stack starts).
    refetchInterval: (query) => (query.state.status === "error" ? 15_000 : 5_000),
    refetchIntervalInBackground: false,
    retry: false,
    staleTime: 4_000,
  });
  const data = q.status === "success" ? q.data : undefined;
  const state: StatusState =
    q.status === "pending" ? "loading" : !data ? "unavailable" : data.all_operational ? "ok" : "degraded";
  return { state, data, updatedAt: q.dataUpdatedAt };
}
