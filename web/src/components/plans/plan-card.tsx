"use client";

import { motion, useReducedMotion } from "motion/react";
import { ChevronDown, CornerDownRight, Lock, ShieldAlert, Workflow } from "lucide-react";
import { ExecutionTimeline } from "@/components/approval/execution-timeline";
import { Interpretation } from "@/components/approval/approval-card";
import { IssuesList } from "@/components/approval/issues";
import { PriceBandMeter } from "@/components/approval/price-band";
import { ProposalActions } from "@/components/approval/proposal-actions";
import { FieldRows } from "@/components/approval/provenance";
import { Chip, LookChip, SideBadge } from "@/components/ui/chip";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Hash } from "@/components/ui/hash";
import { Eyebrow } from "@/components/ui/panel";
import { inr, price, qty, time } from "@/lib/format";
import { PROPOSAL_STATUS, STEP_STATE } from "@/lib/status";
import type { Proposal, ProposalStep } from "@/lib/types";
import { cn } from "@/lib/utils";

const SIGNED: Proposal["status"][] = ["APPROVED", "EXECUTING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED"];

function StepRow({ step, proposal, defaultOpen }: { step: ProposalStep; proposal: Proposal; defaultOpen?: boolean }) {
  const c = step.canonical;
  const state = STEP_STATE[step.state ?? "PENDING"];
  const ob = proposal.execution?.outbox.find((o) => o.step_no === step.step_no);
  const awaiting = proposal.status === "AWAITING_APPROVAL";
  const blockers = step.issues.filter((i) => i.severity === "BLOCK").length;
  return (
    <Collapsible defaultOpen={defaultOpen} className="rounded-lg bg-surface-2/50 ring-1 ring-hairline">
      <CollapsibleTrigger className="group/step flex w-full items-center gap-3 px-3 py-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span
          className={cn(
            "num grid size-7 shrink-0 place-items-center rounded-full text-[12px] font-semibold ring-1",
            step.state === "FILLED" ? "bg-profit/15 text-profit ring-profit/40" : "bg-surface-3 ring-hairline-strong",
          )}
        >
          {step.step_no}
        </span>
        <SideBadge side={c.side} size="sm" />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="font-semibold">{c.instrument.symbol}</span>
            <span className="num text-[13px]">
              {qty(c.quantity)} <span className="unit">@</span> {c.limit_price_paise ? price(c.limit_price_paise) : `MKT (${price(c.protection_price_paise)})`}
            </span>
          </div>
          <p className="num truncate text-[11px] text-subtle">
            {c.order_type} · {c.product} · {c.validity}
            {step.preview ? ` · value ${inr(step.preview.order_value_paise)}` : ""}
            {ob?.client_order_id ? ` · tag ${ob.client_order_id}` : ""}
          </p>
        </div>
        {blockers > 0 && (
          <Chip tone="danger" size="xs" icon={Lock}>
            {blockers} block
          </Chip>
        )}
        <LookChip look={state} size="xs" />
        <ChevronDown className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[panel-open]/step:rotate-180" aria-hidden />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="space-y-3 border-t border-hairline p-3">
          <p className="num text-[13px] font-medium">{step.summary}</p>
          <FieldRows fields={step.fields} disagreements={proposal.interpretation.disagreements} />
          {step.preview && (
            <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
              {[
                ["Order value", inr(step.preview.order_value_paise)],
                ["Est. margin", inr(step.preview.estimated_margin_paise)],
                ["Est. charges", inr(step.preview.estimated_charges_paise)],
                ["Position", `${qty(step.preview.current_position_qty)} to ${qty(step.preview.resulting_position_qty)}`],
              ].map(([k, v]) => (
                <div key={k} className="rounded-md bg-background/40 px-2 py-1.5 ring-1 ring-hairline">
                  <div className="text-[10.5px] text-muted-foreground">{k}</div>
                  <div className="num font-medium">{v}</div>
                </div>
              ))}
            </div>
          )}
          {step.preview && <PriceBandMeter preview={step.preview} instrumentId={c.instrument.instrument_id} active={awaiting} />}
          {step.issues.length > 0 && <IssuesList issues={step.issues} />}
          {SIGNED.includes(proposal.status) && <ExecutionTimeline proposal={proposal} stepNo={step.step_no} />}
          {step.reason && <p className="text-xs text-warning">{step.reason}</p>}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

export function PlanCard({ proposal, className }: { proposal: Proposal; className?: string }) {
  const reduce = useReducedMotion();
  const look = PROPOSAL_STATUS[proposal.status];
  const signed = proposal.approval?.status === "VERIFIED" && SIGNED.includes(proposal.status);
  const awaiting = proposal.status === "AWAITING_APPROVAL";
  const blocked = proposal.status === "BLOCKED";
  const buy = proposal.steps.filter((s) => s.canonical.side === "BUY").reduce((a, s) => a + (s.preview?.order_value_paise ?? 0), 0);
  const sell = proposal.steps.filter((s) => s.canonical.side === "SELL").reduce((a, s) => a + (s.preview?.order_value_paise ?? 0), 0);
  const n = proposal.steps.length;

  return (
    <motion.article
      initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 420, damping: 34 }}
      aria-label={`Plan approval card ${proposal.short_hash}`}
      className={cn(
        "relative overflow-hidden rounded-xl bg-surface-1 ring-1",
        blocked ? "ring-danger/40" : awaiting ? "ring-human/35" : "ring-hairline-strong",
        className,
      )}
    >
      <div className={cn("h-[3px]", blocked ? "bg-danger" : awaiting ? "bg-[linear-gradient(90deg,var(--ai),var(--human)_45%)]" : "bg-info")} aria-hidden />
      <header className="flex flex-wrap items-start gap-3 px-4 pt-3.5">
        <span className="grid size-8 place-items-center rounded-lg bg-info/12 text-info ring-1 ring-info/30">
          <Workflow className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-base leading-none font-semibold">Plan</h3>
            <Chip tone="info" size="xs">
              {n} steps
            </Chip>
            {signed && (
              <Chip tone="human" size="xs" icon={Lock}>
                Signed plan is immutable
              </Chip>
            )}
          </div>
          <p className="mt-1 text-xs text-subtle">Created {time(proposal.created_at)}</p>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <LookChip look={look} size="md" />
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            Plan ID <Hash value={proposal.canonical_hash} display={proposal.short_hash} className="text-foreground" label="plan hash" />
          </span>
        </div>
      </header>

      <div className="mt-3 space-y-2 px-4">
        <Interpretation proposal={proposal} />
        <div className="flex items-start gap-2 rounded-lg bg-warning/[0.07] px-3 py-2 text-xs ring-1 ring-warning/25">
          <ShieldAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
          <span>
            <span className="font-semibold text-warning">Stop on failure: no automatic compensating trades.</span>{" "}
            <span className="text-muted-foreground">If a step does not fill, later steps are skipped and nothing is reversed for you.</span>
          </span>
        </div>
      </div>

      <div className="space-y-1.5 p-4">
        <Eyebrow>Steps, in order</Eyebrow>
        {proposal.steps.map((s, i) => (
          <div key={s.step_no} className="space-y-1.5">
            {s.depends_on && (
              <div className="flex items-center gap-2 pl-3 text-[11px] text-muted-foreground">
                <CornerDownRight className="size-3.5 text-info" aria-hidden />
                runs after step {s.depends_on} fills
              </div>
            )}
            <StepRow step={s} proposal={proposal} defaultOpen={i === 0 && awaiting && n <= 2 ? false : undefined} />
          </div>
        ))}
        <div className="flex flex-wrap justify-end gap-x-4 gap-y-1 pt-1 text-xs text-muted-foreground">
          {buy > 0 && (
            <span>
              Buys <span className="num text-foreground">{inr(buy)}</span>
            </span>
          )}
          {sell > 0 && (
            <span>
              Sells <span className="num text-foreground">{inr(sell)}</span>
            </span>
          )}
          <span>
            Net cash <span className="num text-foreground">{inr(sell - buy, { sign: "always" })}</span>
          </span>
        </div>
      </div>

      {proposal.interpretation.disagreements.length > 0 && (
        <div className="px-4 pb-3">
          <IssuesList issues={[]} disagreements={proposal.interpretation.disagreements} />
        </div>
      )}

      {(awaiting || blocked) && (
        <footer className="border-t border-hairline bg-surface-2/40 px-4 py-3">
          <ProposalActions
            proposal={proposal}
            countersignLabel="Countersign plan"
            caption={
              <>
                <span className="text-foreground">One signature binds all {n} steps.</span> It is single-use, expires in 60 s, and the signed
                plan cannot be edited afterwards.
              </>
            }
          />
        </footer>
      )}
    </motion.article>
  );
}
