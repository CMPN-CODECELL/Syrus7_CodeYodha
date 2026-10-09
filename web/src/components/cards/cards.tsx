"use client";

import Link from "next/link";
import { ArrowUpRight, CircleAlert, CircleCheck, Info, MessageCircleQuestion, OctagonX } from "lucide-react";
import { ApprovalCard } from "@/components/approval/approval-card";
import { MandateCard } from "@/components/mandates/mandate-card";
import { OptionChainTable } from "@/components/market/option-chain-table";
import { PlanCard } from "@/components/plans/plan-card";
import { HoldingsTable, OrdersTable, PositionsTable } from "@/components/portfolio/tables";
import { Kpi } from "@/components/ui/kpi";
import { LiveChange, LivePrice } from "@/components/ui/live-price";
import { Sparkline } from "@/components/ui/sparkline";
import { ErrorState, SkeletonCard } from "@/components/ui/states";
import { displayAccountId } from "@/lib/broker";
import { compactCount, date, price, time } from "@/lib/format";
import { useHistory, useProposal } from "@/lib/queries";
import { useChatStore, useLiveStore } from "@/lib/store";
import type { AccountSummary, Card, InstrumentHandle, OptionChain, Tick } from "@/lib/types";
import { cn } from "@/lib/utils";

function CardFrame({
  title,
  meta,
  href,
  children,
  className,
  bodyClassName,
}: {
  title: string;
  meta?: React.ReactNode;
  href?: string;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn("overflow-hidden rounded-xl bg-surface-1 ring-1 ring-hairline-strong", className)}>
      <header className="flex h-10 items-center gap-2 border-b border-hairline px-3.5">
        <h3 className="truncate text-[13px] font-semibold">{title}</h3>
        {meta && <span className="truncate text-[11px] text-subtle">{meta}</span>}
        {href && (
          <Link href={href} className="ml-auto inline-flex items-center gap-0.5 text-[11px] text-muted-foreground hover:text-foreground">
            Open <ArrowUpRight className="size-3" />
          </Link>
        )}
      </header>
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

export function AccountSummaryCard({ data }: { data: AccountSummary }) {
  return (
    <CardFrame title="Account summary" meta={`${displayAccountId(data.account_id)} · as of ${time(data.as_of)}`} href="/portfolio">
      <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3">
        <Kpi label="Day P&L" paise={data.day_pnl_paise} signed emphasis />
        <Kpi label="Total P&L" paise={data.total_pnl_paise} signed emphasis />
        <Kpi label="Available margin" paise={data.available_margin_paise} />
        <Kpi label="Used margin" paise={data.used_margin_paise} />
        <Kpi label="Holdings value" paise={data.holdings_value_paise} sub={`${data.holdings_count} holdings`} />
        <Kpi label="Exposure" paise={data.exposure_paise} sub={`${data.positions_count} positions · ${data.open_orders_count} open orders`} />
      </div>
    </CardFrame>
  );
}

function QuoteCard({ data }: { data: { instrument: InstrumentHandle; tick: Tick } }) {
  const history = useHistory(data.instrument.symbol);
  const live = useLiveStore((s) => s.ticks[data.instrument.instrument_id]) ?? data.tick;
  const values = (history.data ?? []).slice(-90).map((p) => p.value);
  return (
    <CardFrame title={data.instrument.symbol} meta={data.instrument.display_name} href="/market">
      <div className="flex flex-wrap items-end gap-4 p-3.5">
        <div>
          <div className="text-[26px] leading-none font-semibold tracking-tight">
            <LivePrice instrumentId={data.instrument.instrument_id} fallback={data.tick.ltp_paise} symbol />
          </div>
          <div className="mt-1.5 flex items-center gap-2 text-xs">
            <LiveChange instrumentId={data.instrument.instrument_id} fallbackBps={data.tick.change_bps} />
            <span className="text-subtle">vs prev close {price(live.prev_close_paise)}</span>
          </div>
        </div>
        <div className="ml-auto">
          {values.length > 1 ? <Sparkline values={values} width={180} height={48} /> : <div className="h-12 w-[180px] animate-pulse rounded bg-muted" />}
        </div>
      </div>
      <dl className="grid grid-cols-3 border-t border-hairline text-xs">
        {[
          ["Bid", price(live.bid_paise)],
          ["Ask", price(live.ask_paise)],
          ["Volume", compactCount(live.volume)],
        ].map(([k, v]) => (
          <div key={k} className="border-r border-hairline px-3.5 py-2 last:border-r-0">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="num font-medium">{v}</dd>
          </div>
        ))}
      </dl>
    </CardFrame>
  );
}

function OptionChainCard({ data }: { data: OptionChain }) {
  return (
    <CardFrame
      title="NIFTY option chain"
      meta={`Expiry ${date(data.expiry)} · lot ${data.lot_size} · spot ${price(data.spot_paise)}`}
      href="/market"
    >
      <div className="max-h-[420px] overflow-auto">
        <OptionChainTable chain={data} compact />
      </div>
    </CardFrame>
  );
}

function ClarifyCard({ question, options }: { question: string; options: string[] }) {
  const send = useChatStore((s) => s.send);
  const streaming = useChatStore((s) => s.streaming);
  return (
    <section className="rounded-xl bg-ai/[0.06] p-3.5 ring-1 ring-ai/25">
      <p className="flex items-start gap-2 text-sm font-medium">
        <MessageCircleQuestion className="mt-0.5 size-4 shrink-0 text-ai" />
        {question}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {options.map((o) => (
          <button
            key={o}
            type="button"
            disabled={streaming}
            onClick={() => void send(o)}
            className="min-h-9 rounded-lg bg-surface-1 px-3 text-left text-[13px] ring-1 ring-hairline-strong transition-colors hover:ring-ai/50 disabled:opacity-50"
          >
            {o}
          </button>
        ))}
      </div>
    </section>
  );
}

function NoticeCard({ tone, title, message }: { tone: "info" | "warning" | "danger" | "success"; title: string; message: string }) {
  const Icon = tone === "danger" ? OctagonX : tone === "warning" ? CircleAlert : tone === "success" ? CircleCheck : Info;
  const cls = {
    info: "bg-info/[0.07] ring-info/25 text-info",
    warning: "bg-warning/[0.08] ring-warning/30 text-warning",
    danger: "bg-danger/[0.08] ring-danger/30 text-danger",
    success: "bg-profit/[0.08] ring-profit/25 text-profit",
  }[tone];
  return (
    <section role={tone === "danger" ? "alert" : "status"} className={cn("flex items-start gap-2.5 rounded-xl p-3.5 ring-1", cls)}>
      <Icon className="mt-0.5 size-4 shrink-0" />
      <div className="space-y-0.5">
        <p className="text-sm font-semibold">{title}</p>
        <p className="text-[13px] text-foreground/85">{message}</p>
      </div>
    </section>
  );
}

/** Proposal cards are always rendered from GET /api/proposals/{id}, never from chat text. */
export function ProposalCard({ id }: { id: string }) {
  const { data, error, isPending, refetch } = useProposal(id);
  if (isPending) return <SkeletonCard lines={6} />;
  if (error) return <ErrorState error={error} title="Could not load this proposal" onRetry={() => void refetch()} />;
  if (data.kind === "PLAN") return <PlanCard proposal={data} />;
  if (data.kind === "MANDATE" && data.mandate) return <MandateCard mandate={data.mandate} proposal={data} />;
  return <ApprovalCard proposal={data} />;
}

export function CardRenderer({ card }: { card: Card }) {
  switch (card.kind) {
    case "account_summary":
      return <AccountSummaryCard data={card.data} />;
    case "holdings":
      return (
        <CardFrame title={card.title} meta={`${card.data.length} ${card.data.length === 1 ? "holding" : "holdings"}`} href="/portfolio">
          <HoldingsTable data={card.data} compact sortable={false} maxHeight="360px" />
        </CardFrame>
      );
    case "positions":
      return (
        <CardFrame title={card.title} meta={`${card.data.length} positions`} href="/portfolio">
          <PositionsTable data={card.data} maxHeight="320px" />
        </CardFrame>
      );
    case "orders":
      return (
        <CardFrame title={card.title} meta={`${card.data.length} orders`} href="/portfolio">
          <OrdersTable data={card.data} compact maxHeight="360px" />
        </CardFrame>
      );
    case "quote":
      return <QuoteCard data={card.data} />;
    case "option_chain":
      return <OptionChainCard data={card.data} />;
    case "proposal":
      return <ProposalCard id={card.proposal_id} />;
    case "clarify":
      return <ClarifyCard question={card.question} options={card.options} />;
    case "notice":
      return <NoticeCard tone={card.tone} title={card.title} message={card.message} />;
    default:
      return null;
  }
}

