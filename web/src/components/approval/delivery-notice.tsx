"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ArrowUpRight, CircleSlash, RotateCcw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { redraftText } from "@/lib/broker";
import { useBrokerKindFor } from "@/lib/queries";
import { useChatStore, useUiStore } from "@/lib/store";
import type { Proposal, ProposalStep } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ambiguousText, deliveryState, notPlacedText } from "./execution-timeline";

/**
 * Asks the Copilot to draft the same order again. Drafting never sends anything: the result is a new proposal with a
 * fresh price band that waits for a fresh countersignature.
 */
export function useRedraft() {
  const router = useRouter();
  const pathname = usePathname();
  const requestComposer = useUiStore((s) => s.requestComposer);
  return (text: string) => {
    if (pathname !== "/copilot") router.push("/copilot");
    requestComposer(text, { send: true });
  };
}

/** Clear wording for a step the broker never accepted (not placed) or never confirmed (needs review). */
export function DeliveryNotice({ proposal, step, className }: { proposal: Proposal; step: ProposalStep; className?: string }) {
  const ob = proposal.execution?.outbox.find((o) => o.step_no === step.step_no);
  const state = deliveryState(step, ob);
  const kind = useBrokerKindFor(step.canonical.account_id);
  const redraft = useRedraft();
  const streaming = useChatStore((s) => s.streaming);
  if (!state) return null;

  if (state === "NOT_PLACED") {
    const text = redraftText(step.canonical, proposal.interpretation.user_text);
    return (
      <div role="status" className={cn("space-y-2.5 rounded-lg bg-danger/[0.06] p-3 ring-1 ring-danger/30", className)}>
        <div className="flex items-start gap-2">
          <CircleSlash className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
          <div className="min-w-0 space-y-0.5">
            <p className="text-[13px] font-semibold text-danger">Not placed. Nothing was sent twice.</p>
            <p className="text-xs leading-snug text-foreground/85">{step.reason || notPlacedText(kind)}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 pl-6">
          <Button size="sm" variant="outline" disabled={streaming} onClick={() => redraft(text)}>
            <RotateCcw /> Draft it again
          </Button>
          <p className="min-w-0 flex-1 text-[11px] leading-snug text-muted-foreground">
            Drafts <q className="text-foreground/85">{text}</q> as a new proposal with a fresh price band. Nothing is sent until you
            countersign it.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div role="status" className={cn("space-y-2.5 rounded-lg bg-warning/[0.07] p-3 ring-1 ring-warning/35", className)}>
      <div className="flex items-start gap-2">
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
        <div className="min-w-0 space-y-0.5">
          <p className="text-[13px] font-semibold text-warning">Needs your review</p>
          <p className="text-xs leading-snug text-foreground/85">{step.reason || ambiguousText(kind)}</p>
        </div>
      </div>
      <div className="pl-6">
        <Link
          href="/portfolio"
          className="inline-flex min-h-8 items-center gap-1 rounded-lg px-2.5 text-xs font-medium ring-1 ring-hairline-strong transition-colors outline-none hover:bg-foreground/5 focus-visible:ring-2 focus-visible:ring-ring"
        >
          Open the order book <ArrowUpRight className="size-3.5" aria-hidden />
        </Link>
      </div>
    </div>
  );
}
