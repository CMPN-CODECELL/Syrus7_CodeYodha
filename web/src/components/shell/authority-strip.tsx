"use client";

import Link from "next/link";
import { PenLine, Power, ShieldCheck, Sparkles } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { time } from "@/lib/format";
import { useSecurityStatus } from "@/lib/queries";
import { useLiveStore } from "@/lib/store";
import { cn } from "@/lib/utils";

const CHIP = "inline-flex h-6 shrink-0 items-center gap-1 rounded-full px-2 text-[10.5px] font-semibold tracking-[0.04em] uppercase ring-1 ring-inset outline-none focus-visible:ring-2 focus-visible:ring-ring sm:gap-1.5 sm:px-2.5";

/** Compact statement of who may do what, shown where trades are drafted (the Copilot header). */
export function AuthorityChips({ className }: { className?: string }) {
  const { data } = useSecurityStatus();
  const liveMonitor = useLiveStore((s) => s.monitor);
  const unapproved = liveMonitor?.unapproved_executions ?? data?.monitor.unapproved_executions;
  return (
    <div className={cn("flex min-w-0 items-center gap-1.5", className)}>
      <Tooltip>
        <TooltipTrigger render={<span tabIndex={0} className={cn(CHIP, "bg-ai/10 text-ai ring-ai/30")} />}>
          <Sparkles className="size-3" aria-hidden />
          <span>
            AI: propose<span className="hidden sm:inline"> only</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-sm">
          The model can read your account and market data and draft proposals. It has no order tools and never holds a broker write key.
        </TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger render={<span tabIndex={0} className={cn(CHIP, "bg-human/10 text-human ring-human/35")} />}>
          <PenLine className="size-3" aria-hidden />
          <span className="sm:hidden">You sign</span>
          <span className="hidden sm:inline">Execution: your countersignature</span>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-sm">
          Only a fresh, single-use countersignature from you (passkey, PIN or authenticator code) lets the executor place an order. It expires in 60 s.
        </TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger
          render={
            <Link
              href="/attack-lab"
              aria-label={`Unapproved trades: ${unapproved ?? "unknown"}`}
              className={cn(
                CHIP,
                "num tracking-normal normal-case",
                unapproved === undefined
                  ? "text-muted-foreground ring-hairline"
                  : unapproved === 0
                    ? "bg-profit/10 text-profit ring-profit/30"
                    : "bg-danger/15 text-danger ring-danger/40",
              )}
            />
          }
        >
          <ShieldCheck className="size-3" aria-hidden />
          <span className="hidden font-sans md:inline">Unapproved trades</span>
          {unapproved ?? "-"}
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-sm">
          Counted independently by the invariant monitor: broker orders without a verified, consumed capability. It must stay 0, even under attack.
        </TooltipContent>
      </Tooltip>
    </div>
  );
}

export function KillSwitchBanner() {
  const { data } = useSecurityStatus();
  if (!data?.kill_switch.engaged) return null;
  return (
    <div role="alert" className="flex shrink-0 items-center gap-3 bg-danger px-4 py-2 text-[13px] text-white">
      <Power className="size-4 shrink-0 animate-pulse" aria-hidden />
      <p className="min-w-0 flex-1">
        <span className="font-semibold tracking-wide uppercase">Kill switch engaged.</span>{" "}
        All approvals and executions are halted
        {data.kill_switch.at ? ` since ${time(data.kill_switch.at)}` : ""}.
        {data.kill_switch.reason ? ` Reason: ${data.kill_switch.reason}.` : ""}
      </p>
      <Link
        href="/security#kill-switch"
        className="shrink-0 rounded-md bg-white/15 px-2.5 py-1 text-xs font-medium hover:bg-white/25"
      >
        Open Security Center
      </Link>
    </div>
  );
}
