"use client";

import { CircleCheck, CircleDashed, CircleSlash, FingerprintPattern, type LucideIcon } from "lucide-react";
import { useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { useInterval, useLive, useReducedMotion } from "../hooks";

// An example three-step plan under one countersignature. Step 2 only partly fills, so the
// executor stops there and step 3 is skipped (stop on failure, no compensating trades).

type State = "filled" | "partial" | "skipped";

const LOOK: Record<State, { label: string; icon: LucideIcon; chip: string; rail: string }> = {
  filled: { label: "Filled", icon: CircleCheck, chip: "bg-profit/12 text-profit ring-profit/25", rail: "bg-profit" },
  partial: { label: "Partially filled", icon: CircleDashed, chip: "bg-warning/12 text-warning ring-warning/30", rail: "bg-warning" },
  skipped: { label: "Skipped", icon: CircleSlash, chip: "bg-foreground/[0.05] text-muted-foreground ring-hairline-strong", rail: "bg-muted-foreground/50" },
};

const STEPS: { order: string; limit: string; after: string; state: State; detail: string }[] = [
  {
    order: "SELL 50 TATAMOTORS",
    limit: "₹864.00 limit",
    after: "runs first",
    state: "filled",
    detail: "50 of 50 filled. The plan moves on to step 2.",
  },
  {
    order: "BUY 20 INFY",
    limit: "₹1,771.00 limit",
    after: "after step 1",
    state: "partial",
    detail: "12 of 20 filled. A step that does not fully succeed stops every step after it.",
  },
  {
    order: "BUY 10 ITC",
    limit: "₹445.00 limit",
    after: "after step 2",
    state: "skipped",
    detail: "Never sent, because step 2 did not complete. The plan ends partially completed.",
  },
];

export function PlanDemo() {
  const ref = useRef<HTMLDivElement>(null);
  const live = useLive(ref);
  const reduced = useReducedMotion();
  const [auto, setAuto] = useState(1);
  const [hover, setHover] = useState<number | null>(null);
  const active = hover ?? auto;
  useInterval(() => setAuto((i) => (i + 1) % STEPS.length), 2600, live && hover === null && !reduced);

  return (
    <div ref={ref} className="flex gap-3 sm:gap-4" onMouseLeave={() => setHover(null)}>
      <ol className="min-w-0 flex-1 space-y-0" aria-label="Example plan steps">
        {STEPS.map((s, i) => {
          const look = LOOK[s.state];
          const on = i === active;
          return (
            <li key={s.order} className="relative">
              {i > 0 && (
                <svg aria-hidden className="ml-[18px] block h-6 w-3 overflow-visible">
                  <line x1="1" x2="1" y1="0" y2="24" stroke="var(--hairline-strong)" strokeWidth="1.5" className="lp-flow" />
                </svg>
              )}
              <button
                type="button"
                onMouseEnter={() => setHover(i)}
                onFocus={() => setHover(i)}
                onBlur={() => setHover(null)}
                aria-describedby={on ? "lp-plan-detail" : undefined}
                className={cn(
                  "relative flex w-full items-center gap-3 overflow-hidden rounded-lg border px-3 py-2.5 text-left transition-[border-color,background-color] duration-300 outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  on ? "border-hairline-strong bg-foreground/[0.04]" : "border-hairline bg-background/40",
                )}
              >
                <span className={cn("absolute inset-y-0 left-0 w-[3px] transition-opacity", look.rail, on ? "opacity-100" : "opacity-40")} aria-hidden />
                <span className="num grid size-6 shrink-0 place-items-center rounded-full bg-foreground/[0.07] text-[11px] font-semibold">{i + 1}</span>
                <span className="min-w-0 flex-1">
                  <span className="num block truncate text-[13px] font-semibold">{s.order}</span>
                  <span className="block text-[11px] leading-tight text-subtle">
                    <span className="num">{s.limit}</span> · {s.after}
                  </span>
                </span>
                <span className={cn("inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[10.5px] font-medium ring-1 ring-inset", look.chip)}>
                  <look.icon className="size-3" aria-hidden />
                  <span className="max-sm:sr-only">{look.label}</span>
                </span>
              </button>
            </li>
          );
        })}
        <li className="pt-3">
          <p id="lp-plan-detail" className="min-h-[40px] text-[12.5px] leading-snug text-muted-foreground" aria-live="off">
            <span className="font-medium text-foreground">Step {active + 1}: </span>
            {STEPS[active].detail}
          </p>
        </li>
      </ol>

      {/* One signature spans the whole plan. */}
      <div className="hidden w-[84px] shrink-0 flex-col items-center sm:flex" aria-hidden>
        <div className="w-px flex-1 bg-gradient-to-b from-transparent via-human/60 to-human/60" />
        <div className="my-2 flex flex-col items-center gap-1.5 rounded-lg border border-human/30 bg-human/[0.08] px-2 py-2.5 text-center">
          <FingerprintPattern className="size-5 text-human" />
          <span className="text-[10.5px] leading-tight font-medium text-human">1 signature</span>
          <span className="text-[9.5px] leading-tight text-subtle">over all 3 steps</span>
        </div>
        <div className="w-px flex-1 bg-gradient-to-b from-human/60 via-human/60 to-transparent" />
      </div>
    </div>
  );
}
