"use client";

import { motion, useReducedMotion } from "motion/react";
import { Bot, MessageSquareQuote, ShieldCheck } from "lucide-react";
import { Chip, LookChip, SideBadge } from "@/components/ui/chip";
import { Hash } from "@/components/ui/hash";
import { Eyebrow } from "@/components/ui/panel";
import { time } from "@/lib/format";
import { proposalLook } from "@/lib/status";
import type { Proposal } from "@/lib/types";
import { cn } from "@/lib/utils";
import { DeliveryNotice } from "./delivery-notice";
import { deliveryState, ExecutionTimeline } from "./execution-timeline";
import { IssuesList } from "./issues";
import { PriceBandMeter } from "./price-band";
import { ProposalActions } from "./proposal-actions";
import { FieldRows } from "./provenance";
import { WalletPreview } from "./wallet-preview";

const POST_APPROVAL: Proposal["status"][] = ["APPROVED", "EXECUTING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "NEEDS_REVIEW"];

/**
 * Readable engine label. Accepts "deterministic", "compromised-model", "provider:model" and
 * fallback chains such as "groq:openai/gpt-oss-20b+gemini:gemini-2.5-flash".
 */
export function engineLabel(engine: string): { label: string; tone: "ai" | "neutral" | "danger"; full: string } {
  if (engine === "deterministic") return { label: "Deterministic parser", tone: "neutral", full: engine };
  if (engine.startsWith("deterministic")) {
    return { label: `Deterministic${engine.slice("deterministic".length)}`, tone: "neutral", full: engine };
  }
  if (engine === "compromised-model") return { label: "Compromised model (attack)", tone: "danger", full: engine };
  const chain = engine.split("+").filter(Boolean);
  const [provider, ...rest] = (chain[0] ?? engine).split(":");
  const model = rest.join(":").split("/").pop() ?? "";
  const name = `${provider.charAt(0).toUpperCase()}${provider.slice(1)} ${model}`.trim();
  const label = chain.length > 1 ? `${name} +${chain.length - 1} fallback${chain.length > 2 ? "s" : ""}` : name;
  return { label, tone: "ai", full: chain.join(" then ") };
}

export function Interpretation({ proposal }: { proposal: Proposal }) {
  const eng = engineLabel(proposal.interpretation.engine);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted-foreground">
      <span className="inline-flex min-w-0 items-center gap-1.5">
        <MessageSquareQuote className="size-3.5 shrink-0 text-human" aria-hidden />
        From your words:
        <q className="truncate font-medium text-foreground">{proposal.interpretation.user_text}</q>
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Bot className="size-3.5 text-ai" aria-hidden />
        Drafted by
        <Chip tone={eng.tone} size="xs" className="max-w-[260px] normal-case" title={eng.full}>
          <span className="truncate">{eng.label}</span>
        </Chip>
        <span className="text-subtle">checked by the deterministic verifier</span>
      </span>
    </div>
  );
}

/** The approval card. Rendered only from GET /api/proposals/{id}; never from chat text. */
export function ApprovalCard({ proposal, className }: { proposal: Proposal; className?: string }) {
  const reduce = useReducedMotion();
  const step = proposal.steps[0];
  if (!step) return null;
  const c = step.canonical;
  const look = proposalLook(proposal.status);
  const blocked = proposal.status === "BLOCKED";
  const awaiting = proposal.status === "AWAITING_APPROVAL";
  const executed = POST_APPROVAL.includes(proposal.status);
  const delivery = deliveryState(step, proposal.execution?.outbox.find((o) => o.step_no === step.step_no));
  const review = proposal.status === "NEEDS_REVIEW" || delivery === "AMBIGUOUS";

  return (
    <motion.article
      initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 420, damping: 34 }}
      aria-label={`Order approval card ${proposal.short_hash}`}
      className={cn(
        "@container relative overflow-hidden rounded-xl bg-surface-1 ring-1",
        blocked ? "ring-danger/40" : awaiting ? "ring-human/35" : "ring-hairline-strong",
        className,
      )}
    >
      {/* Status rail across the top */}
      <div
        className={cn(
          "h-[3px] w-full",
          blocked
            ? "bg-danger"
            : awaiting
              ? "bg-[linear-gradient(90deg,var(--ai),var(--human)_45%,var(--human))]"
              : proposal.status === "COMPLETED"
                ? "bg-profit"
                : proposal.status === "FAILED" || delivery === "NOT_PLACED"
                  ? "bg-danger"
                  : review || proposal.status === "PARTIALLY_COMPLETED"
                    ? "bg-warning"
                    : "bg-info",
        )}
        aria-hidden
      />

      <header className="flex flex-wrap items-start gap-3 px-4 pt-3.5">
        <SideBadge side={c.side} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <h3 className="text-lg leading-none font-semibold tracking-tight">{c.instrument.symbol}</h3>
            <span className="num truncate text-[11px] text-muted-foreground">{c.instrument.instrument_id}</span>
          </div>
          <p className="mt-1 truncate text-xs text-subtle">
            {c.instrument.display_name}
            {c.instrument.isin ? ` · ${c.instrument.isin}` : ""}
            {c.instrument.flags.length ? ` · ${c.instrument.flags.join(", ")}` : ""}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <LookChip look={look} size="md" />
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <Chip tone="neutral" size="xs">
              {c.action}
            </Chip>
            <span>Order ID</span>
            <Hash value={proposal.canonical_hash} display={proposal.short_hash} className="text-foreground" label="order hash" />
          </div>
        </div>
      </header>

      <div className="mt-3 space-y-2 px-4">
        <p className="num rounded-lg bg-surface-2/70 px-3 py-2 text-[14px] leading-snug font-semibold ring-1 ring-hairline">{step.summary}</p>
        <Interpretation proposal={proposal} />
      </div>

      <div className="grid gap-4 p-4 @2xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-2">
          <Eyebrow className="flex items-center justify-between">
            <span>Order fields</span>
            <span className="font-normal tracking-normal normal-case text-subtle">Hover a source to see where it came from</span>
          </Eyebrow>
          <FieldRows fields={step.fields} disagreements={proposal.interpretation.disagreements} />
        </div>
        {step.preview && <WalletPreview preview={step.preview} instrumentId={c.instrument.instrument_id} side={c.side} />}
      </div>

      {step.preview && (
        <div className="px-4 pb-3">
          <PriceBandMeter preview={step.preview} instrumentId={c.instrument.instrument_id} active={awaiting} />
        </div>
      )}

      {(proposal.issues.length > 0 || proposal.interpretation.disagreements.length > 0) && (
        <div className="space-y-2 px-4 pb-4">
          <Eyebrow>Verifier findings</Eyebrow>
          <IssuesList issues={proposal.issues} disagreements={proposal.interpretation.disagreements} />
        </div>
      )}

      {executed && (
        <div className="mx-4 mb-4 rounded-lg bg-surface-2/50 p-3.5 ring-1 ring-hairline">
          <div className="mb-3 flex items-center justify-between">
            <Eyebrow className="flex items-center gap-1.5 text-human">
              <ShieldCheck className="size-3.5" /> Execution
            </Eyebrow>
            {proposal.execution && <span className="num text-[11px] text-subtle">job {proposal.execution.job_id}</span>}
          </div>
          <ExecutionTimeline proposal={proposal} />
          {delivery && <DeliveryNotice proposal={proposal} step={step} className="mt-3" />}
        </div>
      )}

      {(awaiting || blocked) && (
        <footer className="border-t border-hairline bg-surface-2/40 px-4 py-3">
          <ProposalActions
            proposal={proposal}
            caption={
              <>
                Your countersignature is a <span className="text-foreground">signed, single-use</span> approval of hash{" "}
                <span className="num text-foreground">{proposal.short_hash}</span> that <span className="text-foreground">expires in 60 s</span>. The AI
                cannot sign.
              </>
            }
          />
        </footer>
      )}
      {!awaiting && !blocked && !executed && (
        <footer className="border-t border-hairline px-4 py-2.5 text-xs text-muted-foreground">
          {look.label} · updated {time(proposal.updated_at)}
        </footer>
      )}
    </motion.article>
  );
}
