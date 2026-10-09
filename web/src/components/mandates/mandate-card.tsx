"use client";

import { motion, useReducedMotion } from "motion/react";
import { Bell, Crosshair, Repeat1, TriangleAlert, X } from "lucide-react";
import { toast } from "sonner";
import { Interpretation } from "@/components/approval/approval-card";
import { ProposalActions, useRevise } from "@/components/approval/proposal-actions";
import { Button } from "@/components/ui/button";
import { Chip, LookChip, SideBadge } from "@/components/ui/chip";
import { Hash } from "@/components/ui/hash";
import { Eyebrow } from "@/components/ui/panel";
import { errorMessage } from "@/lib/errors";
import { dateTime, pct, price, qty, until } from "@/lib/format";
import { useCancelMandate, useHoldings, useProposal } from "@/lib/queries";
import { mandateLook } from "@/lib/status";
import { useLiveStore, useTick } from "@/lib/store";
import type { MandateView, Proposal } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

/** Instrument id of a standing alert that watches every holding instead of one stock. */
const ALL_HOLDINGS_ID = "PORTFOLIO:HOLDINGS";

/** Live distance between the trigger and the current price. */
export function TriggerDistance({ mandate }: { mandate: MandateView }) {
  const tick = useTick(mandate.instrument.instrument_id);
  const holdings = useHoldings();
  const pred = mandate.predicate;
  const ltp = tick?.ltp_paise;
  if (pred.field === "DAY_CHANGE_BPS" && mandate.instrument.instrument_id === ALL_HOLDINGS_ID) {
    return (
      <div className="rounded-lg bg-surface-2/50 px-3 py-2 text-xs leading-snug text-muted-foreground ring-1 ring-hairline">
        Watches every stock you hold and alerts when one is {pct(Math.abs(pred.threshold))} or more {pred.threshold < 0 ? "below" : "above"} its
        previous close on the day.
      </div>
    );
  }
  let threshold: number | null = null;
  if (pred.field === "LTP") threshold = pred.threshold;
  else if (pred.field === "DAY_CHANGE_BPS") {
    const prev = tick?.prev_close_paise;
    threshold = prev ? Math.round(prev * (1 + pred.threshold / 10000)) : null;
  } else {
    const h = holdings.data?.find((x) => x.instrument.instrument_id === mandate.instrument.instrument_id);
    if (h) threshold = Math.round(h.avg_price_paise * (1 - pred.threshold / 10000));
  }
  if (!ltp || !threshold) {
    return (
      <div className="h-[52px] rounded-lg bg-surface-2/50 px-3 py-2 text-xs text-muted-foreground ring-1 ring-hairline">
        {pred.field === "DROP_FROM_AVG_BPS"
          ? `Fires when price is ${pct(pred.threshold)} below your average price.`
          : pred.field === "DAY_CHANGE_BPS"
            ? `Fires when price is ${pct(Math.abs(pred.threshold))} ${pred.threshold < 0 ? "below" : "above"} the previous close.`
            : "Waiting for a live tick..."}
      </div>
    );
  }
  // DROP_FROM_AVG_BPS compares the drop (GTE means "fell at least this far"), i.e. price at or below the trigger.
  const priceOp = pred.field === "DROP_FROM_AVG_BPS" ? (pred.op === "GTE" ? "LTE" : "GTE") : pred.op;
  const distBps = priceOp === "LTE" ? ((ltp - threshold) / ltp) * 10000 : ((threshold - ltp) / ltp) * 10000;
  const met = distBps <= 0;
  const fill = met ? 100 : Math.max(4, Math.min(100, 100 - distBps / 10));
  const tone = met ? "danger" : distBps < 100 ? "warning" : distBps < 300 ? "info" : "muted";
  const bar = { danger: "bg-danger", warning: "bg-warning", info: "bg-info", muted: "bg-foreground/30" }[tone];
  return (
    <div className="rounded-lg bg-surface-2/50 px-3 py-2 ring-1 ring-hairline">
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-muted-foreground">
          Trigger <span className="num text-foreground">{price(threshold)}</span>
        </span>
        <span className={cn("num font-medium", met ? "text-danger" : tone === "warning" ? "text-warning" : "text-foreground")}>
          {met ? "Condition met" : `${(distBps / 100).toFixed(2)}% ${priceOp === "LTE" ? "above" : "below"} trigger`}
        </span>
        <span className="text-muted-foreground">
          LTP <span className="num text-foreground">{price(ltp)}</span>
        </span>
      </div>
      <div className="relative mt-1.5 h-1.5 overflow-hidden rounded-full bg-foreground/[0.07]" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(fill)} aria-label="Closeness to trigger">
        <div className={cn("absolute inset-y-0 left-0 rounded-full transition-[width] duration-500", bar, met && "animate-pulse")} style={{ width: `${fill}%` }} />
      </div>
    </div>
  );
}

export function MandateCard({ mandate: initial, proposal, className }: { mandate: MandateView; proposal?: Proposal; className?: string }) {
  const reduce = useReducedMotion();
  const live = useLiveStore((s) => s.mandates[initial.mandate_id]);
  const m = live ?? initial;
  const now = useNow(15_000);
  const cancel = useCancelMandate();
  const revise = useRevise();
  const source = useProposal(proposal ? null : m.proposal_id);
  const p = proposal ?? source.data;
  const look = mandateLook(m.state);
  const awaitingApproval = p?.status === "AWAITING_APPROVAL" || p?.status === "BLOCKED";
  const cancellable = ["ACTIVE", "NEEDS_REVIEW"].includes(m.state) && !awaitingApproval;
  const t = m.template;

  return (
    <motion.article
      initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 420, damping: 34 }}
      aria-label={`Standing instruction on ${m.instrument.symbol}`}
      className={cn(
        "relative flex flex-col overflow-hidden rounded-xl bg-surface-1 ring-1",
        m.state === "NEEDS_REVIEW" ? "ring-warning/40" : m.state === "ACTIVE" ? "ring-human/30" : "ring-hairline-strong",
        className,
      )}
    >
      <header className="flex items-start gap-3 px-4 pt-3.5">
        <span
          className={cn(
            "grid size-8 shrink-0 place-items-center rounded-lg ring-1",
            m.kind === "ALERT" ? "bg-warning/10 text-warning ring-warning/30" : "bg-human/10 text-human ring-human/30",
          )}
        >
          {m.kind === "ALERT" ? <Bell className="size-4" /> : <Crosshair className="size-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <h3 className="font-semibold">{m.instrument.symbol}</h3>
            <Chip tone="neutral" size="xs">
              {m.kind === "ALERT" ? "Alert only" : "Order mandate"}
            </Chip>
            {m.one_shot && (
              <Chip tone="neutral" size="xs" icon={Repeat1}>
                One-shot
              </Chip>
            )}
          </div>
          <p className="mt-0.5 text-[11px] text-subtle">
            {m.kind === "ALERT" ? "Notifies you; never trades" : "Standing instruction"} · expires {until(m.expires_at, now)}
          </p>
        </div>
        <LookChip look={look} size="md" />
      </header>

      <div className="space-y-3 p-4">
        <div>
          <Eyebrow>When</Eyebrow>
          <p className="num mt-1 text-[17px] font-semibold tracking-tight">{m.condition_text}</p>
        </div>
        {["ACTIVE", "PENDING_APPROVAL", "NEEDS_REVIEW", "TRIGGERED"].includes(m.state) && <TriggerDistance mandate={m} />}

        {t && (
          <div>
            <Eyebrow>Then</Eyebrow>
            <div className="mt-1 flex items-center gap-2 rounded-lg bg-surface-2/50 px-3 py-2 ring-1 ring-hairline">
              <SideBadge side={t.side} size="sm" />
              <span className="num text-[13px] font-medium">
                {qty(t.quantity)} {t.instrument.symbol} {t.limit_price_paise ? `@ ${price(t.limit_price_paise)}` : "at market (protected)"} {t.order_type}
              </span>
              <span className="ml-auto text-[11px] text-subtle">
                {t.product} · {t.validity}
              </span>
            </div>
          </div>
        )}

        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-muted-foreground">Max quantity</dt>
            <dd className="num font-medium">{m.max_quantity !== null ? qty(m.max_quantity) : "-"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Price limit</dt>
            <dd className="num font-medium">{m.price_limit_paise ? price(m.price_limit_paise) : "-"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Expires</dt>
            <dd className="num font-medium">{dateTime(m.expires_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Armed</dt>
            <dd className="num font-medium">{dateTime(m.activated_at)}</dd>
          </div>
          {m.fired_at && (
            <div>
              <dt className="text-muted-foreground">Fired</dt>
              <dd className="num font-medium text-profit">{dateTime(m.fired_at)}</dd>
            </div>
          )}
          {m.trigger_event_id && (
            <div className="col-span-2 sm:col-span-1">
              <dt className="text-muted-foreground">Trigger event</dt>
              <dd>
                <Hash value={m.trigger_event_id} className="text-[11px]" label="trigger event id" />
              </dd>
            </div>
          )}
        </dl>

        {m.state === "NEEDS_REVIEW" && (
          <div className="flex items-start gap-2 rounded-lg bg-warning/[0.08] px-3 py-2.5 text-xs ring-1 ring-warning/30">
            <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" />
            <div className="space-y-1">
              <p className="font-medium text-warning">Price crossed while the system was offline; it will not fire stale. Review and countersign again.</p>
              {m.reason && <p className="text-muted-foreground">{m.reason}</p>}
            </div>
          </div>
        )}
        {m.state === "FAILED" && m.reason && <p className="text-xs text-danger">{m.reason}</p>}
        {p && proposal && <Interpretation proposal={p} />}
      </div>

      {p && awaitingApproval ? (
        <footer className="mt-auto border-t border-hairline bg-surface-2/40 px-4 py-3">
          <ProposalActions
            proposal={p}
            countersignLabel="Countersign to arm"
            caption={
              m.instrument.instrument_id === ALL_HOLDINGS_ID
                ? "Arming needs your signature once. It alerts at most once per holding per trading day, only on a fresh tick, and expires on its own."
                : "Arming needs your signature once. It fires at most once, only on a fresh tick, and expires on its own."
            }
          />
        </footer>
      ) : (
        (cancellable || m.state === "NEEDS_REVIEW") && (
          <footer className="mt-auto flex flex-wrap items-center gap-2 border-t border-hairline px-4 py-2.5">
            {m.state === "NEEDS_REVIEW" && p && (
              <Button size="sm" className="bg-human text-background hover:bg-human/90" onClick={() => revise(p.interpretation.user_text)}>
                Review and countersign again
              </Button>
            )}
            {cancellable && (
              <Button
                size="sm"
                variant="outline"
                className="ml-auto"
                disabled={cancel.isPending}
                onClick={() =>
                  cancel.mutate(m.mandate_id, {
                    onSuccess: () => toast("Mandate cancelled", { description: `${m.instrument.symbol}: ${m.condition_text} will not fire.` }),
                    onError: (e) => toast.error("Could not cancel the mandate", { description: errorMessage(e) }),
                  })
                }
              >
                <X /> Cancel mandate
              </Button>
            )}
          </footer>
        )
      )}
    </motion.article>
  );
}
