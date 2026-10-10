"use client";

import { motion, useReducedMotion } from "motion/react";
import { ChevronDown, CornerDownRight, Lock, ShieldAlert, Split, XCircle } from "lucide-react";
import { DeliveryNotice } from "@/components/approval/delivery-notice";
import { deliveryState, ExecutionTimeline } from "@/components/approval/execution-timeline";
import { Interpretation } from "@/components/approval/approval-card";
import { IssuesList } from "@/components/approval/issues";
import { PriceBandMeter } from "@/components/approval/price-band";
import { ProposalActions } from "@/components/approval/proposal-actions";
import { FieldRows } from "@/components/approval/provenance";
import { Chip, LookChip, SideBadge } from "@/components/ui/chip";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Hash } from "@/components/ui/hash";
import { Eyebrow } from "@/components/ui/panel";
import { realOrderId } from "@/lib/broker";
import { inr, price, qty, time } from "@/lib/format";
import { useBrokerKindFor } from "@/lib/queries";
import { proposalLook, stepLook } from "@/lib/status";
import type { Proposal, ProposalStep } from "@/lib/types";
import { cn } from "@/lib/utils";
import { PLAN_KIND_LOOK, PLAN_NOTE_RE, PlanMath, PlanNotes, planKind, planTitle, tallySteps } from "./plan-math";

const SIGNED: Proposal["status"][] = ["APPROVED", "EXECUTING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "NEEDS_REVIEW"];

const STEP_BADGE: Partial<Record<string, string>> = {
  FILLED: "bg-profit/15 text-profit ring-profit/40",
  PARTIALLY_FILLED: "bg-warning/15 text-warning ring-warning/40",
  AMBIGUOUS: "bg-warning/15 text-warning ring-warning/40",
  NEEDS_REVIEW: "bg-warning/15 text-warning ring-warning/40",
  REJECTED: "bg-danger/15 text-danger ring-danger/40",
  FAILED: "bg-danger/15 text-danger ring-danger/40",
  NOT_PLACED: "bg-danger/15 text-danger ring-danger/40",
};

function StepRow({ step, proposal, defaultOpen }: { step: ProposalStep; proposal: Proposal; defaultOpen?: boolean }) {
  const c = step.canonical;
  const state = stepLook(step.state);
  const ob = proposal.execution?.outbox.find((o) => o.step_no === step.step_no);
  const delivery = deliveryState(step, ob);
  const partial = step.state === "PARTIALLY_FILLED" && step.filled_quantity !== undefined;
  // 021 has no client order tag; the outbox id never leaves Countersign, so it is not shown as a broker tag.
  const tagged = useBrokerKindFor(c.account_id) !== "021" && !!ob?.client_order_id;
  const awaiting = proposal.status === "AWAITING_APPROVAL";
  const blockers = step.issues.filter((i) => i.severity === "BLOCK").length;
  // The plan's own notes are shown once at plan level, not again inside step 1.
  const issues = step.issues.filter((i) => !PLAN_NOTE_RE.test(i.code));
  // For a computed plan, the deciding number for this step (resulting weight or unrealised P&L).
  const key = step.fields.find((f) => f.key === "weight") ?? step.fields.find((f) => f.key === "unrealised_pnl");
  const why = step.rationale ?? (key ? `${key.label} ${key.display}` : null);
  // A CANCEL step reads as a cancel at a glance, never as the BUY or SELL it removes.
  const cancel = c.action === "CANCEL";
  return (
    <Collapsible defaultOpen={defaultOpen} className="rounded-lg bg-surface-2/50 ring-1 ring-hairline">
      <CollapsibleTrigger className="group/step flex w-full items-center gap-3 px-3 py-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span
          className={cn(
            "num grid size-7 shrink-0 place-items-center rounded-full text-[12px] font-semibold ring-1",
            STEP_BADGE[step.state ?? ""] ?? "bg-surface-3 ring-hairline-strong",
          )}
        >
          {step.step_no}
        </span>
        {cancel ? (
          <span className="grid size-6 shrink-0 place-items-center rounded-md bg-danger/12 text-danger ring-1 ring-danger/30" aria-label="Cancel">
            <XCircle className="size-3.5" aria-hidden />
          </span>
        ) : (
          <SideBadge side={c.side} size="sm" />
        )}
        <div className="min-w-0 flex-1">
          {cancel ? (
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-semibold text-danger">Cancel</span>
              <span className="num text-[13px]">{step.summary.replace(/^CANCEL\s+/, "")}</span>
            </div>
          ) : (
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-semibold">{c.instrument.symbol}</span>
              <span className="num text-[13px]">
                {qty(c.quantity)} <span className="unit">@</span> {c.limit_price_paise ? price(c.limit_price_paise) : `MKT (${price(c.protection_price_paise)})`}
              </span>
            </div>
          )}
          <p className="num truncate text-[11px] text-subtle">
            {c.order_type} · {c.product} · {c.validity}
            {step.preview ? ` · value ${inr(step.preview.order_value_paise)}` : ""}
            {tagged ? ` · tag ${ob?.client_order_id}` : ""}
            {realOrderId(step.broker_order_id) ? ` · order ${step.broker_order_id}` : ""}
          </p>
          {why && <p className="num mt-0.5 line-clamp-2 text-[11px] leading-snug text-muted-foreground">{why}</p>}
        </div>
        {blockers > 0 && (
          <Chip tone="danger" size="xs" icon={Lock}>
            {blockers} block
          </Chip>
        )}
        <span className="flex shrink-0 flex-col items-end gap-0.5">
          <LookChip look={state} size="xs" />
          {partial && (
            <span className="num text-[10.5px] text-warning">
              {qty(step.filled_quantity ?? 0)} of {qty(c.quantity)}
            </span>
          )}
        </span>
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
          {issues.length > 0 && <IssuesList issues={issues} />}
          {SIGNED.includes(proposal.status) && <ExecutionTimeline proposal={proposal} stepNo={step.step_no} />}
          {delivery || step.state === "PARTIALLY_FILLED" ? (
            <DeliveryNotice proposal={proposal} step={step} />
          ) : (
            step.reason && <p className="text-xs text-warning">{step.reason}</p>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function ResultStrip({ proposal }: { proposal: Proposal }) {
  const t = tallySteps(proposal.steps);
  const n = proposal.steps.length;
  const parts: { label: string; tone: string }[] = [];
  if (t.filled) parts.push({ label: `${t.filled} filled`, tone: "text-profit" });
  if (t.partial) {
    const partials = proposal.steps.filter((s) => s.state === "PARTIALLY_FILLED");
    const detail = partials.map((s) => `${s.canonical.instrument.symbol} ${qty(s.filled_quantity ?? 0)} of ${qty(s.canonical.quantity)}`).join(", ");
    parts.push({ label: `${t.partial} partially filled (${detail})`, tone: "text-warning" });
  }
  if (t.rejected) parts.push({ label: `${t.rejected} rejected`, tone: "text-danger" });
  if (t.notPlaced) parts.push({ label: `${t.notPlaced} not placed`, tone: "text-danger" });
  if (t.review) parts.push({ label: `${t.review} need${t.review === 1 ? "s" : ""} your review`, tone: "text-warning" });
  if (t.skipped) parts.push({ label: `${t.skipped} skipped`, tone: "text-muted-foreground" });
  if (t.open) parts.push({ label: `${t.open} open`, tone: "text-info" });
  if (t.pending) parts.push({ label: `${t.pending} not started`, tone: "text-muted-foreground" });
  return (
    <div role="status" className="space-y-1 rounded-lg bg-surface-2/60 px-3 py-2.5 ring-1 ring-hairline">
      <p className="text-xs">
        <span className="font-semibold">Result: </span>
        {parts.map((p, i) => (
          <span key={p.label}>
            {i > 0 && <span className="text-subtle">, </span>}
            <span className={cn("num", p.tone)}>{p.label}</span>
          </span>
        ))}
        <span className="text-subtle">
          {" "}
          of {n} step{n === 1 ? "" : "s"}.
        </span>
      </p>
      {(t.rejected > 0 || t.notPlaced > 0 || t.review > 0) && (
        <p className="text-[11px] leading-snug text-muted-foreground">
          Nothing was retried or reversed for you. Open a step to see the broker&apos;s reason
          {t.notPlaced ? ", or to draft a step that was not placed again" : ""}.
        </p>
      )}
    </div>
  );
}

export function PlanCard({ proposal, className }: { proposal: Proposal; className?: string }) {
  const reduce = useReducedMotion();
  const look = proposalLook(proposal.status);
  const signed = proposal.approval?.status === "VERIFIED" && SIGNED.includes(proposal.status);
  const awaiting = proposal.status === "AWAITING_APPROVAL";
  const blocked = proposal.status === "BLOCKED";
  const buy = proposal.steps.filter((s) => s.canonical.side === "BUY").reduce((a, s) => a + (s.preview?.order_value_paise ?? 0), 0);
  const sell = proposal.steps.filter((s) => s.canonical.side === "SELL").reduce((a, s) => a + (s.preview?.order_value_paise ?? 0), 0);
  const n = proposal.steps.length;
  const kind = planKind(proposal);
  const kindLook = PLAN_KIND_LOOK[kind ?? "custom"];
  const title = planTitle(proposal) || (kind ? kindLook.title : "Plan");
  // Plan-level verifier findings other than the computed plan's notes (those render in PlanNotes).
  const planIssues = proposal.issues.filter((i) => !PLAN_NOTE_RE.test(i.code) && i.severity !== "INFO");
  // The executor only skips a step whose dependency did not fill, so steps without dependencies are independent.
  const independent = proposal.failure_policy === "CONTINUE_ON_FAILURE" || (n > 1 && proposal.steps.every((s) => !s.depends_on));
  const started = signed && proposal.steps.some((s) => s.state && s.state !== "PENDING");
  const KindIcon = kindLook.icon;

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
      <div
        className={cn(
          "h-[3px]",
          blocked || proposal.status === "FAILED"
            ? "bg-danger"
            : awaiting
              ? "bg-[linear-gradient(90deg,var(--ai),var(--human)_45%)]"
              : proposal.status === "COMPLETED"
                ? "bg-profit"
                : proposal.status === "PARTIALLY_COMPLETED" || proposal.status === "NEEDS_REVIEW"
                  ? "bg-warning"
                  : "bg-info",
        )}
        aria-hidden
      />
      <header className="flex flex-wrap items-start gap-3 px-4 pt-3.5">
        <span className="grid size-8 place-items-center rounded-lg bg-info/12 text-info ring-1 ring-info/30">
          <KindIcon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base leading-none font-semibold">{title}</h3>
            <Chip tone="info" size="xs">
              {n} step{n === 1 ? "" : "s"}
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
        {independent ? (
          <div className="flex items-start gap-2 rounded-lg bg-info/[0.07] px-3 py-2 text-xs ring-1 ring-info/25">
            <Split className="mt-0.5 size-3.5 shrink-0 text-info" aria-hidden />
            <span>
              <span className="font-semibold text-info">Independent steps: no automatic compensating trades.</span>{" "}
              <span className="text-muted-foreground">
                No step waits for another. If the broker refuses a step when it is sent, the later steps are skipped; a rejection reported after a
                step was accepted does not stop the others. Nothing is retried or reversed for you.
              </span>
            </span>
          </div>
        ) : (
          <div className="flex items-start gap-2 rounded-lg bg-warning/[0.07] px-3 py-2 text-xs ring-1 ring-warning/25">
            <ShieldAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
            <span>
              <span className="font-semibold text-warning">Stop on failure: no automatic compensating trades.</span>{" "}
              <span className="text-muted-foreground">
                If a step that a later one depends on does not fill, or the broker refuses a step when it is sent, later steps are skipped. Nothing is
                reversed for you.
              </span>
            </span>
          </div>
        )}
      </div>

      <PlanNotes proposal={proposal} className="mx-4 mt-3" />
      {proposal.plan && <PlanMath plan={proposal.plan} className="mx-4 mt-3" />}
      {planIssues.length > 0 && (
        <div className="mx-4 mt-3">
          <IssuesList issues={planIssues} />
        </div>
      )}

      <div className="space-y-1.5 p-4">
        {started && <ResultStrip proposal={proposal} />}
        <Eyebrow className={cn(started && "pt-1.5")}>Steps, in order</Eyebrow>
        {n === 0 && (
          <p className="rounded-lg bg-surface-2/50 px-3 py-2.5 text-xs text-muted-foreground ring-1 ring-hairline">
            No orders are needed: nothing in the account matches this plan right now.
          </p>
        )}
        {proposal.steps.map((s, i) => (
          <div key={s.step_no} className="space-y-1.5">
            {s.depends_on && (
              <div className="flex items-center gap-2 pl-3 text-[11px] text-muted-foreground">
                <CornerDownRight className="size-3.5 text-info" aria-hidden />
                runs after step {s.depends_on} fills
              </div>
            )}
            <StepRow
              step={s}
              proposal={proposal}
              defaultOpen={
                deliveryState(s, proposal.execution?.outbox.find((o) => o.step_no === s.step_no)) ? true : i === 0 && awaiting && n <= 2 ? false : undefined
              }
            />
          </div>
        ))}
        {n > 0 && kind !== "cancel_all" && (
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
        )}
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
