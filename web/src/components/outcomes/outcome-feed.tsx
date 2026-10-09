"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { realOrderId } from "@/lib/broker";
import { useRouter } from "next/navigation";
import { ArrowUpRight, Radio } from "lucide-react";
import { TONE_CHIP } from "@/components/ui/chip";
import { EmptyState } from "@/components/ui/states";
import { ago, time } from "@/lib/format";
import { OUTCOME_KIND, SEVERITY_TONE } from "@/lib/status";
import { useChatStore } from "@/lib/store";
import type { OutcomeEvent } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

/** For an order that was not placed or needs review: open its card in the Copilot, where it can be drafted again. */
function OpenOrderAction({ o }: { o: OutcomeEvent }) {
  const router = useRouter();
  const inject = useChatStore((s) => s.inject);
  const pid = o.proposal_id;
  if (!pid || (o.kind !== "ORDER_NOT_PLACED" && o.kind !== "ORDER_NEEDS_REVIEW")) return null;
  const notPlaced = o.kind === "ORDER_NOT_PLACED";
  return (
    <button
      type="button"
      onClick={() => {
        router.push("/copilot");
        inject(
          [{ type: "card", card: { kind: "proposal", proposal_id: pid, status: notPlaced ? "FAILED" : "NEEDS_REVIEW" }, key: "" }],
          notPlaced ? "This order was not placed. You can draft it again from its card; nothing is sent until you countersign." : "This order needs your review:",
        );
      }}
      className="inline-flex items-center gap-0.5 font-semibold tracking-wide text-foreground/80 uppercase underline-offset-2 hover:text-foreground hover:underline"
    >
      Open the order <ArrowUpRight className="size-3" aria-hidden />
    </button>
  );
}

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
          {realOrderId(o.broker_order_id) && <span className="num">· {o.broker_order_id}</span>}
          {o.proposal_id && <span className="num">· {o.proposal_id.slice(0, 14)}</span>}
          <span className="ml-auto">
            <OpenOrderAction o={o} />
          </span>
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
