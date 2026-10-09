"use client";

import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { Ban, Lock, PenLine, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { errorMessage } from "@/lib/errors";
import { useRejectProposal, useSecurityStatus } from "@/lib/queries";
import { useLiveStore, useUiStore } from "@/lib/store";
import type { Proposal } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CountersignDialog } from "./countersign-dialog";

/** Puts text back into the Copilot composer, navigating there if needed. */
export function useRevise() {
  const router = useRouter();
  const pathname = usePathname();
  const requestComposer = useUiStore((s) => s.requestComposer);
  return (text: string) => {
    if (pathname !== "/copilot") router.push("/copilot");
    requestComposer(text);
  };
}

/** Why Countersign is disabled, or null if it can be pressed. Uses live ticks for the band check. */
export function useCountersignBlocker(p: Proposal): string | null {
  const { data: sec } = useSecurityStatus();
  const bandIssue = useLiveStore((s) => {
    if (p.kind === "MANDATE") return null;
    for (const st of p.steps) {
      const pv = st.preview;
      if (!pv) continue;
      const t = s.ticks[st.canonical.instrument.instrument_id];
      if (t?.stale) return "Market data is stale (last tick older than 5 s). Wait for the feed to recover.";
      const ltp = t?.ltp_paise ?? pv.reference_price_paise;
      if (ltp < pv.band_low_paise || ltp > pv.band_high_paise) return "Price left your approved band. This approval is void; review again.";
    }
    return null;
  });
  if (p.status === "BLOCKED" || (p.status === "AWAITING_APPROVAL" && !p.can_approve)) {
    const block = p.issues.find((i) => i.severity === "BLOCK");
    if (p.interpretation.disagreements.length) {
      const d = p.interpretation.disagreements[0];
      return `Blocked: you said ${d.trader_value} for ${d.field}, the AI proposed ${d.model_value}. Revise the instruction.`;
    }
    return block ? `Blocked: ${block.message}` : "The verifier blocked this proposal. Revise the instruction.";
  }
  if (p.status !== "AWAITING_APPROVAL") return `This proposal is ${p.status.toLowerCase().replace(/_/g, " ")}.`;
  if (sec?.kill_switch.engaged) return "Kill switch engaged: all approvals are halted.";
  return bandIssue;
}

export function ProposalActions({
  proposal,
  countersignLabel = "Countersign",
  caption,
  className,
}: {
  proposal: Proposal;
  countersignLabel?: string;
  caption?: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const reject = useRejectProposal();
  const revise = useRevise();
  const blocker = useCountersignBlocker(proposal);
  const actionable = proposal.status === "AWAITING_APPROVAL" || proposal.status === "BLOCKED";
  if (!actionable) return null;
  const blocked = proposal.status === "BLOCKED" || !proposal.can_approve;

  return (
    <div className={cn("space-y-2.5", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          disabled={reject.isPending}
          onClick={() =>
            reject.mutate(proposal.id, {
              onSuccess: () => toast("Proposal rejected", { description: "Nothing was sent to the broker." }),
              onError: (e) => toast.error("Could not reject the proposal", { description: errorMessage(e) }),
            })
          }
        >
          <Ban /> Reject
        </Button>
        <Button variant="ghost" onClick={() => revise(proposal.interpretation.user_text)}>
          <Undo2 /> Revise
        </Button>
        <div className="ml-auto flex items-center">
          {blocker ? (
            <Tooltip>
              <TooltipTrigger render={<span tabIndex={0} className="inline-flex rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring" />}>
                <Button disabled className={cn("pointer-events-none min-w-40", blocked ? "bg-danger/15 text-danger" : "bg-human/40 text-background")}>
                  {blocked ? <Lock /> : <PenLine />} {countersignLabel}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs">
                {blocker}
              </TooltipContent>
            </Tooltip>
          ) : (
            <Button
              onClick={() => setOpen(true)}
              className="min-w-40 bg-human font-semibold text-background shadow-[0_0_0_1px_color-mix(in_oklch,var(--human)_60%,transparent),0_8px_24px_-8px_color-mix(in_oklch,var(--human)_55%,transparent)] hover:bg-human/90"
            >
              <PenLine /> {countersignLabel}
            </Button>
          )}
        </div>
      </div>
      {blocker ? (
        <p className={cn("flex items-start gap-1.5 text-xs", blocked ? "text-danger" : "text-warning")}>
          <Lock className="mt-0.5 size-3 shrink-0" aria-hidden />
          {blocker}
        </p>
      ) : (
        caption && <p className="text-xs text-muted-foreground">{caption}</p>
      )}
      <CountersignDialog proposal={proposal} open={open} onOpenChange={setOpen} />
    </div>
  );
}
