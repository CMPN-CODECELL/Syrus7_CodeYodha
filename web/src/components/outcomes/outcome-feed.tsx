"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Radio } from "lucide-react";
import { TONE_CHIP } from "@/components/ui/chip";
import { EmptyState } from "@/components/ui/states";
import { ago, time } from "@/lib/format";
import { OUTCOME_KIND, SEVERITY_TONE } from "@/lib/status";
import type { OutcomeEvent } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

export function OutcomeItem({ o, now, dense }: { o: OutcomeEvent; now: number; dense?: boolean }) {
  const k = OUTCOME_KIND[o.kind] ?? OUTCOME_KIND.ORDER_ACCEPTED;
  const tone = SEVERITY_TONE[o.severity];
  return (
    <div className={cn("relative flex gap-3", dense ? "px-3 py-2.5" : "px-4 py-3")}>
      <span className={cn("mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg ring-1 ring-inset", TONE_CHIP[tone])}>
        <k.icon className="size-3.5" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <p className="min-w-0 flex-1 truncate text-[13px] font-medium" title={o.title}>
            {o.title}
          </p>
          <time className="num shrink-0 text-[11px] text-subtle" dateTime={o.ts} title={time(o.ts)}>
            {now ? ago(o.ts, now) : time(o.ts)}
          </time>
        </div>
        <p className={cn("mt-0.5 text-xs leading-snug text-muted-foreground", dense && "line-clamp-2")}>{o.detail}</p>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10.5px] text-subtle">
          <span className="font-semibold tracking-wide uppercase">{k.label}</span>
          {o.broker_order_id && <span className="num">· {o.broker_order_id}</span>}
          {o.proposal_id && <span className="num">· {o.proposal_id.slice(0, 14)}</span>}
        </div>
      </div>
    </div>
  );
}

export function OutcomeFeed({ items, dense, emptyHint }: { items: OutcomeEvent[]; dense?: boolean; emptyHint?: string }) {
  const now = useNow(5000);
  const reduce = useReducedMotion();
  if (!items.length) {
    return (
      <EmptyState
        icon={Radio}
        title="No outcomes yet"
        description={emptyHint ?? "Fills, rejections, reconciliations and mandate triggers appear here live, newest first."}
      />
    );
  }
  return (
    <ol className="divide-y divide-hairline" aria-live="polite" aria-label="Live outcomes">
      <AnimatePresence initial={false}>
        {items.map((o) => (
          <motion.li
            key={o.id}
            layout={!reduce}
            initial={reduce ? false : { opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.22 }}
          >
            <OutcomeItem o={o} now={now} dense={dense} />
          </motion.li>
        ))}
      </AnimatePresence>
    </ol>
  );
}
