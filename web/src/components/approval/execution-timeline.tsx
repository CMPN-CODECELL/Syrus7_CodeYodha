"use client";

import { motion, useReducedMotion } from "motion/react";
import { Check, Circle, CircleDashed, X } from "lucide-react";
import { qty, price, shortHash, time } from "@/lib/format";
import type { Proposal, ProposalStep } from "@/lib/types";
import { cn } from "@/lib/utils";

type ItemState = "done" | "active" | "pending" | "failed" | "warn";

interface Item {
  key: string;
  title: string;
  detail?: React.ReactNode;
  state: ItemState;
  at?: string | null;
}

const EXECUTED: Proposal["status"][] = ["APPROVED", "EXECUTING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "ACTIVE"];

function fillItem(step: ProposalStep | undefined): Item {
  const st = step?.state;
  const q = step?.canonical.quantity ?? 0;
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

export function buildTimeline(p: Proposal, stepNo = 1): Item[] {
  const a = p.approval;
  const ex = p.execution;
  const step = p.steps.find((s) => s.step_no === stepNo) ?? p.steps[0];
  const ob = ex?.outbox.find((o) => o.step_no === (step?.step_no ?? 1));
  const verified = a?.status === "VERIFIED";
  const halted = ex?.status === "HALTED" || (ex?.status === "REJECTED" && !ob);
  const methodText = a ? (a.method === "webauthn" ? "Passkey assertion" : a.method === "pin" ? "App PIN" : "Authenticator code") : "";

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
    {
      key: "send",
      title: "Sent to broker with unique tag",
      state: ob ? "done" : ex?.status === "RUNNING" ? "active" : halted ? "failed" : "pending",
      detail: ob ? (
        <>
          Client tag <span className="num">{ob.client_order_id}</span>, attempt {ob.attempts}
        </>
      ) : undefined,
    },
    {
      key: "ack",
      title: ob?.reconciled ? "Reconciled, not retried" : "Broker acknowledged",
      state: ob?.broker_order_id ? "done" : ob && ob.state === "FAILED" ? "failed" : ob ? "active" : "pending",
      detail: ob?.broker_order_id ? (
        <>
          Broker order <span className="num">{ob.broker_order_id}</span>
          {ob.reconciled ? ". The acknowledgement was lost; the executor found the order by its tag and did not resend it." : ""}
        </>
      ) : ob?.reconciled ? (
        "Reconciled by tag: no order at the broker. Not retried automatically."
      ) : undefined,
    },
    fillItem(step),
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
  const items = buildTimeline(proposal, stepNo);
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
