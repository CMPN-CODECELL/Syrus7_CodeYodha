"use client";

import Link from "next/link";
import {
  ArrowRight,
  Bot,
  Cpu,
  HeartPulse,
  Landmark,
  Power,
  RadioTower,
  ShieldCheck,
  Wifi,
  WifiOff,
  type LucideIcon,
} from "lucide-react";
import { Dot } from "@/components/ui/chip";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { brokerHealth } from "@/components/broker/broker-connection";
import { brokerSystemName } from "@/lib/broker";
import { ago, ms, time } from "@/lib/format";
import { useActiveBroker, useSecurityStatus } from "@/lib/queries";
import { useLiveStore } from "@/lib/store";
import type { Tone } from "@/lib/status";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

const FRESH_MS = 20_000;

function fresh(iso: string | null | undefined, now: number) {
  if (!iso || !now) return true;
  return now - new Date(iso).getTime() < FRESH_MS;
}

interface HealthRow {
  key: string;
  label: string;
  icon: LucideIcon;
  tone: Tone;
  value: string;
  detail: string;
}

interface Overall {
  tone: Tone;
  label: string;
}

export interface MarketSource {
  live: boolean;
  /** "Live from the 021 sandbox" or "Replay". */
  label: string;
  title: string;
}

/** Where prices come from for the active account: the 021 market data websocket, or the local replay. */
export function useMarketSource(): MarketSource {
  const broker = useActiveBroker();
  const { data } = useSecurityStatus();
  if (broker.reported) {
    if (broker.live021) {
      return { live: true, label: "Live from the 021 sandbox", title: "Prices stream from the 021 Developer Sandbox market data websocket" };
    }
    return {
      live: false,
      label: "Replay",
      title:
        broker.kind === "021"
          ? "The 021 market feed is not streaming for this account; prices come from the local replay feed"
          : "Prices come from a deterministic tick replay, not the live exchange",
    };
  }
  // Older backend: only the feed mode is known.
  const live = data?.feed.mode === "live";
  return live
    ? { live: true, label: "Live feed", title: "The market feed reports a live session" }
    : { live: false, label: "Replay", title: "Prices come from a deterministic tick replay, not the live exchange" };
}

/** One place that turns GET /api/security/status plus the live stream into rows and a verdict. */
export function useSystemHealth() {
  const { data, error, isPending } = useSecurityStatus();
  const liveMonitor = useLiveStore((s) => s.monitor);
  const conn = useLiveStore((s) => s.conn);
  const retry = useLiveStore((s) => s.retryInMs);
  const now = useNow(2000);
  const monitor = liveMonitor ?? data?.monitor;
  const broker = useActiveBroker();
  const source = useMarketSource();
  const replay = !source.live;
  const account = broker.account;
  const health = account ? brokerHealth(account, now) : null;
  const brokerLabel = brokerSystemName(broker.kind);

  const connRow: HealthRow = {
    key: "live",
    label: "Live connection",
    icon: conn === "offline" ? WifiOff : Wifi,
    tone: conn === "open" ? "profit" : conn === "offline" ? "danger" : conn === "reconnecting" ? "warning" : "muted",
    value:
      conn === "open"
        ? "Connected"
        : conn === "offline"
          ? "Offline"
          : conn === "reconnecting"
            ? `Reconnecting${retry ? ` in ${Math.ceil(retry / 1000)} s` : ""}`
            : "Connecting",
    detail: "Ticks, outcomes, mandates and monitor heartbeats over one stream",
  };

  const rows: HealthRow[] =
    data && monitor
      ? [
          {
            key: "broker",
            label: "Broker",
            icon: Landmark,
            tone: !data.broker.healthy ? "danger" : health ? health.tone : "profit",
            value: !data.broker.healthy
              ? `${brokerLabel} unreachable`
              : health && health.tone !== "profit"
                ? `${brokerLabel}: ${health.label.toLowerCase()}`
                : `${brokerLabel}, ${ms(data.broker.latency_ms)}`,
            detail: !data.broker.healthy
              ? "Executions fail closed until it is back"
              : account?.status.last_error && health?.tone !== "profit"
                ? account.status.last_error
                : broker.kind === "021"
                  ? "021 Developer Sandbox: virtual money on a simulated exchange"
                  : "Local paper broker on a simulated market",
          },
          {
            key: "feed",
            label: "Market feed",
            icon: RadioTower,
            tone: !data.feed.healthy ? "danger" : (data.feed.last_tick_age_ms ?? 0) > 5000 ? "warning" : "profit",
            value: `${source.live ? (broker.kind === "021" ? "Live 021 feed" : "Live feed") : "Replay"}, tick ${ms(data.feed.last_tick_age_ms)} ago`,
            detail: data.feed.healthy ? source.title : "Stale: approvals and mandates pause",
          },
          {
            key: "executor",
            label: "Executor",
            icon: Cpu,
            tone: !data.executor.healthy ? "danger" : fresh(data.executor.last_heartbeat, now) ? "profit" : "warning",
            value: data.executor.healthy
              ? `Heartbeat ${ago(data.executor.last_heartbeat, now || Date.parse(data.executor.last_heartbeat ?? "") || 0)}`
              : "Down",
            detail: "Sole holder of the broker write key",
          },
          {
            key: "monitor",
            label: "Invariant monitor",
            icon: HeartPulse,
            tone:
              !monitor.healthy || monitor.violations > 0 || monitor.unapproved_executions > 0
                ? "danger"
                : fresh(monitor.last_heartbeat, now)
                  ? "profit"
                  : "warning",
            value: `${monitor.violations} violation${monitor.violations === 1 ? "" : "s"}`,
            detail: `${monitor.unapproved_executions} unapproved execution${monitor.unapproved_executions === 1 ? "" : "s"}, heartbeat ${ago(monitor.last_heartbeat, now || Date.parse(monitor.last_heartbeat ?? "") || 0)}`,
          },
          connRow,
          {
            key: "ai",
            label: "AI engine",
            icon: Bot,
            tone: "profit",
            value: data.llm.engine,
            detail: "Drafts proposals only. No order tools, no write key",
          },
          {
            key: "kill",
            label: "Kill switch",
            icon: Power,
            tone: data.kill_switch.engaged ? "danger" : "profit",
            value: data.kill_switch.engaged ? `Engaged${data.kill_switch.at ? ` at ${time(data.kill_switch.at)}` : ""}` : "Off",
            detail: data.kill_switch.engaged ? "All approvals and executions are halted" : "Engage it from the Security Center to halt everything",
          },
        ]
      : [];

  let overall: Overall;
  if (error) overall = { tone: "danger", label: "Status unavailable" };
  else if (isPending || !data) overall = { tone: "muted", label: "Checking systems" };
  else if (data.kill_switch.engaged) overall = { tone: "danger", label: "Kill switch engaged" };
  else {
    const bad = rows.find((r) => r.tone === "danger");
    const warn = rows.find((r) => r.tone === "warning");
    overall = bad
      ? { tone: "danger", label: `${bad.label}: ${bad.value.toLowerCase()}` }
      : warn
        ? { tone: "warning", label: `${warn.label} degraded` }
        : { tone: "profit", label: "All systems normal" };
  }

  return { rows, overall, replay, unapproved: monitor?.unapproved_executions, error, isPending };
}

const PILL_TONE: Record<string, string> = {
  profit: "text-muted-foreground hover:text-foreground",
  warning: "bg-warning/10 text-warning ring-warning/30",
  danger: "bg-danger/15 text-danger ring-danger/40",
  muted: "text-muted-foreground",
};

/** The single status control in the top bar. Opens a popover with every subsystem. */
export function SystemStatus() {
  const { rows, overall, unapproved, error } = useSystemHealth();
  return (
    <Popover>
      <PopoverTrigger
        render={
          <button
            type="button"
            aria-label={`System status: ${overall.label}`}
            className={cn(
              "flex h-9 min-w-9 items-center justify-center gap-2 rounded-lg px-2.5 text-xs font-medium ring-1 ring-hairline transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring data-[popup-open]:bg-foreground/5",
              PILL_TONE[overall.tone] ?? PILL_TONE.muted,
            )}
          />
        }
      >
        <Dot tone={overall.tone} pulse={overall.tone !== "profit"} />
        <span className="hidden max-w-[11rem] truncate sm:inline" aria-hidden>
          {overall.label}
        </span>
      </PopoverTrigger>
      <PopoverContent align="end" sideOffset={8} className="w-[min(360px,calc(100vw-24px))] gap-0 p-0">
        <div className="flex items-start justify-between gap-3 border-b border-hairline px-4 py-3">
          <div className="min-w-0">
            <p className="text-sm font-semibold">System status</p>
            <p className={cn("mt-0.5 flex items-center gap-1.5 text-xs", overall.tone === "profit" ? "text-profit" : overall.tone === "muted" ? "text-muted-foreground" : overall.tone === "warning" ? "text-warning" : "text-danger")}>
              <Dot tone={overall.tone} /> {overall.label}
            </p>
          </div>
          <MarketSourceBadge />
        </div>
        {error ? (
          <p className="px-4 py-3 text-xs text-muted-foreground">The status endpoint did not answer. The stream reconnects on its own.</p>
        ) : (
          <ul className="max-h-[min(60dvh,420px)] divide-y divide-hairline overflow-y-auto">
            {rows.map((r) => (
              <li key={r.key} className="flex items-start gap-3 px-4 py-2.5">
                <r.icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-3">
                    <p className="shrink-0 text-[13px] leading-tight font-medium">{r.label}</p>
                    <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                      <span className="truncate" title={r.value}>
                        {r.value}
                      </span>
                      <Dot tone={r.tone} />
                    </span>
                  </div>
                  <p className="mt-0.5 text-[11px] leading-snug text-subtle">{r.detail}</p>
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="flex items-center justify-between gap-3 border-t border-hairline bg-surface-2/50 px-4 py-3">
          <div>
            <p className="text-[11px] text-muted-foreground">Unapproved trades</p>
            <p className={cn("num text-xl leading-tight font-semibold", unapproved === undefined ? "text-subtle" : unapproved === 0 ? "text-profit" : "text-danger")}>
              {unapproved ?? "-"}
            </p>
          </div>
          <Link
            href="/security"
            className="inline-flex min-h-9 items-center gap-1.5 rounded-lg px-3 text-xs font-medium text-foreground ring-1 ring-hairline-strong transition-colors outline-none hover:bg-foreground/5 focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ShieldCheck className="size-3.5 text-human" aria-hidden /> Security Center <ArrowRight className="size-3.5" aria-hidden />
          </Link>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Live 021 prices vs replay, shown in the status popover, on the Market page and in the Copilot market tab. */
export function MarketSourceBadge({ className }: { className?: string }) {
  const src = useMarketSource();
  return (
    <span
      className={cn(
        "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-[11px] font-medium ring-1 ring-inset",
        src.live ? "bg-profit/10 text-profit ring-profit/25" : "bg-info/10 text-info ring-info/25",
        className,
      )}
      title={src.title}
    >
      <span className={cn("size-1.5 rounded-full", src.live ? "animate-pulse bg-profit" : "bg-info")} />
      {src.label}
    </span>
  );
}

/** Kept for existing imports. */
export const ReplayBadge = MarketSourceBadge;

/** Status of the shared SSE connection to /api/events. */
export function ConnectionIndicator({ className }: { className?: string }) {
  const conn = useLiveStore((s) => s.conn);
  const retry = useLiveStore((s) => s.retryInMs);
  const attempt = useLiveStore((s) => s.connAttempt);
  const tone: Tone = conn === "open" ? "profit" : conn === "offline" ? "danger" : "warning";
  const label =
    conn === "open"
      ? "Live"
      : conn === "offline"
        ? "Offline"
        : conn === "reconnecting"
          ? `Reconnecting${retry ? ` in ${Math.ceil(retry / 1000)} s` : ""}`
          : "Connecting";
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            className={cn(
              "flex h-7 items-center gap-1.5 rounded-lg px-2 text-[11px] font-medium ring-1 ring-hairline",
              conn === "open" ? "text-muted-foreground" : conn === "offline" ? "text-danger" : "text-warning",
              className,
            )}
          />
        }
      >
        {conn === "offline" ? <WifiOff className="size-3.5" /> : conn === "open" ? <Dot tone={tone} pulse /> : <Wifi className="size-3.5 animate-pulse" />}
        <span className="hidden sm:inline">{label}</span>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {conn === "open"
          ? "Live event stream connected: ticks, outcomes, mandates, monitor."
          : `Live event stream ${conn}${attempt ? ` (attempt ${attempt})` : ""}. Data shown may lag; it reconnects automatically with backoff.`}
      </TooltipContent>
    </Tooltip>
  );
}
