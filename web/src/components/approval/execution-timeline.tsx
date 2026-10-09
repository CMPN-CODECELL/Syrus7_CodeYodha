"use client";

import { motion, useReducedMotion } from "motion/react";
import { Check, Circle, CircleDashed, X } from "lucide-react";
import { qty, price, shortHash, time } from "@/lib/format";
import { useBrokerKindFor } from "@/lib/queries";
import type { BrokerKind, ExecutionOutboxRow, Proposal, ProposalStep } from "@/lib/types";
import { cn } from "@/lib/utils";

type ItemState = "done" | "active" | "pending" | "failed" | "warn";

interface Item {
  key: string;
  title: string;
  detail?: React.ReactNode;
  state: ItemState;
  at?: string | null;
}

const EXECUTED: Proposal["status"][] = ["APPROVED", "EXECUTING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "ACTIVE", "NEEDS_REVIEW"];

/** How the executor identifies an order at this broker: a client tag (paper broker) or the order's details (021). */
type Matching = "tag" | "details" | "unknown";

function matchingFor(kind: BrokerKind | null, ob: ExecutionOutboxRow | undefined): Matching {
  if (kind === "021") return "details";
  if (kind === "paper" || ob?.client_order_id) return "tag";
  return "unknown";
}

/** Step outcome as one of the two states that need special wording, from the step or its outbox row. */
export function deliveryState(step: ProposalStep | undefined, ob: ExecutionOutboxRow | undefined): "NOT_PLACED" | "AMBIGUOUS" | null {
  const states = [step?.state, ob?.state];
  if (states.includes("NOT_PLACED")) return "NOT_PLACED";
  if (states.includes("NEEDS_REVIEW") || states.includes("AMBIGUOUS")) return "AMBIGUOUS";
  return null;
}

export function notPlacedText(kind: BrokerKind | null): string {
  return kind === "021"
    ? "The broker returned 503 and the order is not in the 021 order book. Nothing was sent twice. Countersign again to resend."
    : "The broker did not accept the order and it is not in the broker's order book. Nothing was sent twice. Countersign again to resend.";
}

export function ambiguousText(kind: BrokerKind | null): string {
  return kind === "021"
    ? "021 did not confirm this order and the executor could not match it in the 021 order book. Countersign did not resend it. Check the order book before you place it again."
    : "The broker did not confirm this order and the executor could not find it in the order book. Countersign did not resend it. Check the order book before you place it again.";
}

function fillItem(step: ProposalStep | undefined, delivery: "NOT_PLACED" | "AMBIGUOUS" | null): Item {
  const st = step?.state;
  const q = step?.canonical.quantity ?? 0;
  if (delivery === "NOT_PLACED") return { key: "fill", title: "Nothing filled", state: "pending", detail: "No order exists at the broker, so nothing can fill." };
  if (delivery === "AMBIGUOUS") return { key: "fill", title: "Fill unknown", state: "pending", detail: "Shown here once the order is matched in the broker's order book." };
  if (st === "FILLED")
    return {
      key: "fill",
      title: "Filled",
      state: "done",
      detail: `${qty(step?.filled_quantity ?? q)} @ ${price(step?.avg_fill_price_paise)} avg`,
    };
  if (st === "PARTIALLY_FILLED")
    return {
      key: "fill",
      title: "Partially filled",
      state: "warn",
      detail: step?.reason ?? `${qty(step?.filled_quantity ?? 0)} of ${qty(q)} filled; remainder open, nothing retried.`,
    };
  if (st === "REJECTED") return { key: "fill", title: "Rejected by broker", state: "failed", detail: step?.reason ?? "Broker rejected the order. Not retried." };
  if (st === "FAILED") return { key: "fill", title: "Failed", state: "failed", detail: step?.reason ?? "Execution failed. Not retried automatically." };
  if (st === "CANCELLED") return { key: "fill", title: "Cancelled", state: "failed", detail: step?.reason ?? undefined };
  if (st === "SKIPPED") return { key: "fill", title: "Skipped", state: "failed", detail: step?.reason ?? undefined };
  if (st === "OPEN" || st === "SUBMITTED") return { key: "fill", title: "Waiting for fill", state: "active" };
  return { key: "fill", title: "Fill", state: "pending" };
}

export function buildTimeline(p: Proposal, stepNo = 1, kind: BrokerKind | null = null): Item[] {
  const a = p.approval;
  const ex = p.execution;
  const step = p.steps.find((s) => s.step_no === stepNo) ?? p.steps[0];
  const ob = ex?.outbox.find((o) => o.step_no === (step?.step_no ?? 1));
  const verified = a?.status === "VERIFIED";
  const halted = ex?.status === "HALTED" || (ex?.status === "REJECTED" && !ob);
  const methodText = a ? (a.method === "webauthn" ? "Passkey assertion" : a.method === "pin" ? "App PIN" : "Authenticator code") : "";
  const matching = matchingFor(kind, ob);
  const delivery = deliveryState(step, ob);
  const brokerOrderId = ob?.broker_order_id ?? step?.broker_order_id ?? null;

  const sendItem: Item = {
    key: "send",
    title: matching === "tag" ? "Sent to broker with unique tag" : matching === "details" ? "Sent to the 021 sandbox" : "Sent to broker",
    state: ob ? "done" : ex?.status === "RUNNING" ? "active" : halted ? "failed" : "pending",
    detail: ob ? (
      matching === "tag" ? (
        <>
          Client tag <span className="num">{ob.client_order_id}</span>, attempt {ob.attempts}
        </>
      ) : (
        <>
          Recorded in the outbox before sending, attempt {ob.attempts}.
          {matching === "details" ? " 021 has no client order tag, so the executor matches the order's details in the 021 order book." : ""}
        </>
      )
    ) : undefined,
  };

  let ackItem: Item;
  if (delivery === "NOT_PLACED") {
    ackItem = { key: "ack", title: "Not placed", state: "failed", detail: step?.reason || notPlacedText(kind) };
  } else if (delivery === "AMBIGUOUS") {
    ackItem = { key: "ack", title: "Needs your review", state: "warn", detail: step?.reason || ambiguousText(kind) };
  } else {
    const found = matching === "details" ? "matched it in the 021 order book" : "found the order by its tag";
    ackItem = {
      key: "ack",
      title: ob?.reconciled ? "Reconciled, not retried" : "Broker acknowledged",
      state: brokerOrderId ? "done" : ob && ob.state === "FAILED" ? "failed" : ob ? "active" : "pending",
      detail: brokerOrderId ? (
        <>
          Broker order <span className="num">{brokerOrderId}</span>
          {ob?.reconciled ? `. The acknowledgement was lost; the executor ${found} and did not resend it.` : ""}
        </>
      ) : ob?.reconciled ? (
        matching === "details" ? "Reconciled against the 021 order book: no matching order. Not retried automatically." : "Reconciled by tag: no order at the broker. Not retried automatically."
      ) : undefined,
    };
  }

  return [
    {
      key: "sig",
      title: "Signature verified",
      state: verified ? "done" : a && (a.status === "FAILED" || a.status === "EXPIRED") ? "failed" : "pending",
      at: a?.verified_at,
      detail: a ? (
        <>
          {methodText}, {a.assurance === "device_signed" ? "signed on your device" : "attested by the server"}
          {a.method === "webauthn" && a.credential_label ? ` (${a.credential_label})` : ""} over payload <span className="num">{shortHash(a.payload_hash, 12)}</span>
          {a.failure_reason ? `. ${a.failure_reason}` : ""}
        </>
      ) : undefined,
    },
    {
      key: "cap",
      title: "Single-use capability minted",
      state: verified && EXECUTED.includes(p.status) ? "done" : "pending",
      detail: "Bound to this exact signed hash. Expires in 60 s. Can be consumed once.",
    },
    {
      key: "verify",
      title: "Executor re-verified hash, expiry, price band",
      state: halted ? "failed" : ex && ex.status !== "PENDING" && ex.status !== "HELD" ? "done" : p.status === "APPROVED" || ex ? "active" : "pending",
      detail: halted ? (ex?.reason ?? "Executor refused the capability.") : "Recomputed the RFC 8785 hash and checked expiry and the live price against your signed band.",
    },
    sendItem,
    ackItem,
    fillItem(step, delivery),
  ];
}

const ICON: Record<ItemState, React.ReactNode> = {
  done: <Check className="size-3" strokeWidth={3} />,
  active: <CircleDashed className="size-3 animate-spin [animation-duration:2.4s]" />,
  pending: <Circle className="size-2" />,
  failed: <X className="size-3" strokeWidth={3} />,
  warn: <CircleDashed className="size-3" />,
};

export function ExecutionTimeline({ proposal, stepNo, className }: { proposal: Proposal; stepNo?: number; className?: string }) {
  const step = proposal.steps.find((s) => s.step_no === stepNo) ?? proposal.steps[0];
  const kind = useBrokerKindFor(step?.canonical.account_id);
  const items = buildTimeline(proposal, stepNo, kind);
  const reduce = useReducedMotion();
  return (
    <ol className={cn("relative space-y-0", className)} aria-label="Execution timeline">
      {items.map((it, i) => (
        <motion.li
          key={it.key}
          initial={reduce ? false : { opacity: 0, x: -4 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.05, duration: 0.18 }}
          className="relative flex gap-3 pb-3 last:pb-0"
        >
          {i < items.length - 1 && (
            <span
              className={cn(
                "absolute top-5 bottom-0 left-[9px] w-px",
                it.state === "done" ? "bg-human/50" : "bg-hairline-strong",
              )}
              aria-hidden
            />
          )}
          <span
            className={cn(
              "relative z-10 mt-0.5 grid size-[19px] shrink-0 place-items-center rounded-full ring-1",
              it.state === "done" && "bg-human text-background ring-human",
              it.state === "active" && "bg-info/15 text-info ring-info/50",
              it.state === "pending" && "bg-surface-2 text-subtle ring-hairline-strong",
              it.state === "failed" && "bg-danger text-white ring-danger",
              it.state === "warn" && "bg-warning/20 text-warning ring-warning/50",
            )}
          >
            {ICON[it.state]}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-2">
              <span
                className={cn(
                  "text-[13px] font-medium",
                  it.state === "pending" && "text-muted-foreground",
                  it.state === "failed" && "text-danger",
                  it.state === "warn" && "text-warning",
                )}
              >
                {it.title}
              </span>
              {it.at && <span className="num ml-auto text-[11px] text-subtle">{time(it.at)}</span>}
            </div>
            {it.detail && <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{it.detail}</p>}
          </div>
        </motion.li>
      ))}
    </ol>
  );
}
