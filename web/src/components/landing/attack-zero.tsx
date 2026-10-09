"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { count } from "./format";
import { usePublicStatus } from "./hooks";

/**
 * Countersign's side of the comparison. Shows the monitor's live count when the public status
 * is reachable; otherwise the recorded result of our local run (docs/ATTACK_LAB.md), labelled as such.
 */
export function AttackZero({ chips }: { chips?: ReactNode }) {
  const { data } = usePublicStatus();
  const value = data ? data.unapproved_executions : 0;
  const clean = value === 0;
  return (
    <div className="relative flex flex-col justify-between gap-6 overflow-hidden bg-[radial-gradient(90%_90%_at_0%_100%,var(--lp-glow),transparent_65%)] p-7 sm:p-10">
      <p className="flex items-center gap-2 text-[13px] font-semibold tracking-wide text-human uppercase">Countersign</p>
      <div>
        <p
          className={cn(
            "font-display text-[clamp(6.5rem,17vw,12rem)] leading-[0.85] [text-shadow:0_0_60px_color-mix(in_oklch,var(--human)_45%,transparent)]",
            clean ? "text-human" : "text-danger",
          )}
        >
          {count(value)}
        </p>
        <p className="mt-4 max-w-sm text-[15px] leading-relaxed text-muted-foreground">
          unapproved executions under attack, counted by the independent monitor against the broker&apos;s real order book.
        </p>
      </div>
      <div className="space-y-3">
        {chips}
        <p className="inline-flex items-start gap-2 text-[12.5px] text-subtle">
          <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", data ? "bg-profit" : "bg-muted-foreground")} aria-hidden />
          {data
            ? `Live from the monitor: ${count(data.attacks_blocked)} of ${count(data.attacks_attempted)} attacks blocked.`
            : "Recorded result of our local run on 8 Oct 2026: 9 of 9 scenarios blocked."}
        </p>
      </div>
    </div>
  );
}
