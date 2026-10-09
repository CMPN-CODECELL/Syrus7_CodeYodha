"use client";

import { Check, Search, Send, ShieldCheck, Unplug, type LucideIcon } from "lucide-react";
import { useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { useInterval, useLive, useReducedMotion } from "../hooks";

// The lost-response drill (Attack Lab E): the broker accepts the order but the reply never
// arrives. The executor looks the order up by its unique client tag instead of blindly resending.

const STEPS: { label: string; note: string; icon: LucideIcon; tone: "info" | "warning" | "human" }[] = [
  { label: "Send", note: "tag CS7B848E5E87CF78", icon: Send, tone: "info" },
  { label: "Response lost", note: "broker accepted, reply dropped", icon: Unplug, tone: "warning" },
  { label: "Look up by tag", note: "query the broker order book", icon: Search, tone: "info" },
  { label: "Found", note: "order 261000004860", icon: Check, tone: "human" },
  { label: "Not resent", note: "exactly 1 order at the broker", icon: ShieldCheck, tone: "human" },
];

const TONE = {
  info: "text-info bg-info/12 ring-info/25",
  warning: "text-warning bg-warning/12 ring-warning/30",
  human: "text-human bg-human/12 ring-human/30",
};

export function ReconcileDemo() {
  const ref = useRef<HTMLDivElement>(null);
  const live = useLive(ref);
  const reduced = useReducedMotion();
  const [tick, setTick] = useState(4);
  useInterval(() => setTick((t) => (t + 1) % (STEPS.length + 2)), 1300, live && !reduced);
  // Two extra ticks hold the final state before the sequence restarts.
  const step = Math.min(STEPS.length - 1, tick);
  const found = step >= 3;
  const searching = step === 2;

  return (
    <div ref={ref} className="grid items-center gap-7 sm:grid-cols-[minmax(0,200px)_1fr] lg:grid-cols-1">
      <div className="relative mx-auto aspect-square w-full max-w-[200px] lg:max-w-[230px]" aria-hidden>
        {[1, 0.8, 0.6, 0.4, 0.2].map((s) => (
          <span
            key={s}
            className="absolute rounded-full border border-human/25"
            style={{ inset: `${((1 - s) / 2) * 100}%` }}
          />
        ))}
        <span className="absolute inset-x-0 top-1/2 h-px bg-human/15" />
        <span className="absolute inset-y-0 left-1/2 w-px bg-human/15" />
        <span className={cn("lp-radar-sweep absolute inset-0 rounded-full transition-opacity duration-500", searching || found ? "opacity-100" : "opacity-40")} />
        <span className="absolute top-1/2 left-1/2 grid size-10 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border border-human/40 bg-background text-human">
          <Search className="size-4" />
        </span>
        {/* The broker's copy of the order, revealed once the tag lookup finds it. */}
        <span
          className={cn(
            "absolute top-[26%] left-[66%] transition-[opacity,transform] duration-500",
            found ? "scale-100 opacity-100" : "scale-50 opacity-0",
          )}
        >
          <span className="lp-ping absolute inset-0 rounded-full bg-human" />
          <span className="relative block size-3 rounded-full bg-human shadow-[0_0_16px_var(--human)]" />
        </span>
      </div>

      <ol className="space-y-1.5" aria-label="Reconcile before retry, step by step">
        {STEPS.map((s, i) => {
          const done = i < step;
          const on = i === step;
          return (
            <li
              key={s.label}
              aria-current={on ? "step" : undefined}
              className={cn(
                "flex items-center gap-3 rounded-lg px-2.5 py-1.5 transition-[background-color,opacity] duration-300",
                on ? "bg-foreground/[0.05]" : done ? "opacity-80" : "opacity-40",
              )}
            >
              <span className={cn("grid size-7 shrink-0 place-items-center rounded-full ring-1 ring-inset", on || done ? TONE[s.tone] : "text-muted-foreground ring-hairline-strong")}>
                <s.icon className="size-3.5" aria-hidden />
              </span>
              <span className="min-w-0">
                <span className="block text-[13px] font-medium">{s.label}</span>
                <span className="num block truncate text-[11px] text-muted-foreground">{s.note}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
