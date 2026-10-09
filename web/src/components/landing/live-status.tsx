"use client";

import { Activity, CircleOff } from "lucide-react";
import { useSyncExternalStore, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { bpsPct, count, price } from "./format";
import { usePublicStatus, type LandingStatus, type StatusState } from "./hooks";

const SYSTEMS = ["api", "broker", "feed", "executor", "monitor"] as const;

// "Updated n s ago" needs a ticking clock; one shared 1 s timer.
let now = 0;
const subs = new Set<() => void>();
let timer: number | null = null;
function subscribe(cb: () => void) {
  subs.add(cb);
  if (timer === null) {
    now = Date.now();
    timer = window.setInterval(() => {
      now = Date.now();
      subs.forEach((s) => s());
    }, 1000);
  }
  return () => {
    subs.delete(cb);
    if (!subs.size && timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
}
function useNow() {
  return useSyncExternalStore(
    subscribe,
    () => now,
    () => 0,
  );
}

/** Small status pill: green "Live", amber "Degraded", grey "Status unavailable". */
export function StatusPill({ state, className }: { state: StatusState; className?: string }) {
  const look = {
    loading: { dot: "bg-muted-foreground", text: "Connecting", cls: "text-muted-foreground ring-hairline-strong" },
    ok: { dot: "bg-profit", text: "Live", cls: "text-profit ring-profit/30 bg-profit/[0.08]" },
    degraded: { dot: "bg-warning", text: "Live, degraded", cls: "text-warning ring-warning/30 bg-warning/[0.08]" },
    unavailable: { dot: "bg-muted-foreground", text: "Status unavailable", cls: "text-muted-foreground ring-hairline-strong" },
  }[state];
  const pulsing = state === "ok" || state === "degraded";
  return (
    <span className={cn("inline-flex items-center gap-2 rounded-full px-2.5 py-1 text-[11.5px] font-medium ring-1", look.cls, className)}>
      <span className="relative flex size-2">
        {pulsing && <span className={cn("lp-ping absolute inset-0 rounded-full", look.dot)} />}
        <span className={cn("relative size-2 rounded-full", look.dot)} />
      </span>
      {look.text}
    </span>
  );
}

function Metric({ value, label, hint, tone }: { value: ReactNode; label: string; hint?: string; tone?: "human" | "danger" }) {
  return (
    <div className="flex flex-col justify-between gap-3 bg-background/60 p-5">
      <p className="text-[12px] leading-snug font-medium text-muted-foreground">{label}</p>
      <div>
        <p
          className={cn(
            "font-display text-[44px] leading-none tracking-tight tabular-nums",
            tone === "human" && "text-human",
            tone === "danger" && "text-danger",
          )}
        >
          {value}
        </p>
        {hint && <p className="mt-1.5 text-[11.5px] text-subtle">{hint}</p>}
      </div>
    </div>
  );
}

// A quiet bar where a number would be: no placeholder digits are ever shown.
const Missing = () => (
  <span className="inline-block h-[0.32em] w-[1.6em] rounded-full bg-foreground/10 align-middle">
    <span className="sr-only">not available</span>
  </span>
);

/** NIFTY prices this page has seen, coloured by the day's change so it agrees with the percentage beside it. */
function Sparkline({ points, up }: { points: number[]; up: boolean }) {
  const w = 160;
  const h = 40;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points
    .map((v, i) => `${i ? "L" : "M"}${((i / (points.length - 1)) * w).toFixed(1)} ${(h - 4 - ((v - min) / span) * (h - 8)).toFixed(1)}`)
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={cn("h-10 w-40", up ? "text-profit" : "text-loss")} aria-hidden>
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function Market({ data }: { data: LandingStatus }) {
  const nifty = data.market?.nifty;
  if (!nifty) return <p className="text-[12.5px] text-muted-foreground">No market data in the status feed yet.</p>;
  const pts = data.niftyHistory;
  return (
    <div className="flex flex-wrap items-center justify-between gap-4">
      <div>
        <p className="text-[12px] text-muted-foreground">
          NIFTY 50 <span className="text-subtle">· replayed market</span>
        </p>
        <p className="num mt-1 text-[20px] font-semibold">
          {price(nifty.ltp_paise)}{" "}
          <span className={cn("text-[13px] font-medium", nifty.change_bps >= 0 ? "text-profit" : "text-loss")}>{bpsPct(nifty.change_bps)}</span>
        </p>
      </div>
      {pts.length >= 2 ? (
        <Sparkline points={pts} up={nifty.change_bps >= 0} />
      ) : (
        <span className="text-[11.5px] text-subtle">Collecting samples from each poll…</span>
      )}
    </div>
  );
}

/** "Live from the independent monitor": real counters from GET /api/public/status, polled every 5 s. */
export function LiveStatusPanel() {
  const { state, data, updatedAt } = usePublicStatus();
  const t = useNow();
  const ago = data && t ? Math.max(0, Math.round((t - updatedAt) / 1000)) : null;
  const up = data ? SYSTEMS.filter((s) => data.systems?.[s]).length : null;

  return (
    <div className="lp-card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-5 py-4 sm:px-6">
        <div className="flex items-center gap-2.5">
          <Activity className="size-4 text-human" aria-hidden />
          <h3 className="text-[15px] font-semibold">Live from the independent monitor</h3>
        </div>
        <div className="flex items-center gap-3">
          {ago !== null && <span className="num text-[11.5px] text-subtle">updated {ago}s ago</span>}
          <StatusPill state={state} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-px bg-hairline">
        <Metric
          label="Unapproved executions"
          value={data ? count(data.unapproved_executions) : <Missing />}
          tone={data ? (data.unapproved_executions === 0 ? "human" : "danger") : undefined}
          hint="broker orders without a countersignature"
        />
        <Metric
          label="Attacks blocked"
          value={
            data ? (
              <>
                {count(data.attacks_blocked)}
                <span className="text-[24px] text-subtle"> / {count(data.attacks_attempted)}</span>
              </>
            ) : (
              <Missing />
            )
          }
          hint="blocked of attempted, Attack Lab"
        />
        <Metric label="Audit events" value={data ? count(data.audit_events) : <Missing />} hint="hash-chained, append only" />
        <Metric
          label="Systems up"
          value={
            up !== null ? (
              <>
                {up}
                <span className="text-[24px] text-subtle"> / {SYSTEMS.length}</span>
              </>
            ) : (
              <Missing />
            )
          }
          hint="api, broker, feed, executor, monitor"
        />
      </div>

      <div className="space-y-4 border-t border-hairline px-5 py-5 sm:px-6">
        <ul className="flex flex-wrap gap-x-4 gap-y-2" aria-label="Processes">
          {SYSTEMS.map((s) => {
            const ok = data ? !!data.systems?.[s] : null;
            return (
              <li key={s} className="inline-flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
                <span className={cn("size-1.5 rounded-full", ok === null ? "bg-foreground/20" : ok ? "bg-profit" : "bg-danger")} aria-hidden />
                <span className="font-mono">{s}</span>
                <span className="sr-only">{ok === null ? "unknown" : ok ? "up" : "down"}</span>
              </li>
            );
          })}
        </ul>
        {data ? (
          <Market data={data} />
        ) : state === "loading" ? (
          <p className="text-[13px] text-muted-foreground">Connecting to the public status feed…</p>
        ) : (
          <div className="flex gap-3 rounded-xl bg-foreground/[0.03] p-4 ring-1 ring-hairline">
            <CircleOff className="mt-0.5 size-4 shrink-0 text-subtle" aria-hidden />
            <p className="text-[13px] leading-relaxed text-muted-foreground">
              The public status feed is not reachable from this page right now, so these numbers are left blank rather than
              filled with placeholders. When the Countersign stack is running, this panel shows the monitor&apos;s real
              counters and refreshes every 5 seconds.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
