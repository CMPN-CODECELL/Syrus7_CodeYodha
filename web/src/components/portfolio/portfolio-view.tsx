"use client";

import { Briefcase, ListOrdered, Package, Layers, XCircle } from "lucide-react";
import { useState } from "react";
import { useRedraft } from "@/components/approval/delivery-notice";
import { Button } from "@/components/ui/button";
import { Kpi } from "@/components/ui/kpi";
import { PageContainer, PageHeader, Panel } from "@/components/ui/panel";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/ui/states";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { pct, time } from "@/lib/format";
import type { OrdersFilter } from "@/lib/api";
import { useAccountSummary, useActiveBroker, useHoldings, useOrders, usePositions } from "@/lib/queries";
import { useChatStore } from "@/lib/store";
import { HoldingsTable, OrdersTable, PositionsTable } from "./tables";

function KpiStrip() {
  const { data, error, isPending, refetch } = useAccountSummary();
  if (error) return <ErrorState error={error} title="Account summary unavailable" onRetry={() => void refetch()} />;
  const usedPct = data ? Math.round((data.used_margin_paise / Math.max(1, data.used_margin_paise + data.available_margin_paise)) * 10000) : 0;
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
      <Kpi label="Total P&L" paise={data?.total_pnl_paise} signed emphasis loading={isPending} sub="vs average purchase price" />
      <Kpi label="Day P&L" paise={data?.day_pnl_paise} signed emphasis loading={isPending} sub="vs previous close" />
      <Kpi label="Available margin" paise={data?.available_margin_paise} loading={isPending} sub={data ? `Cash ${(data.cash_paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 })}` : undefined} />
      <Kpi label="Used margin" paise={data?.used_margin_paise} loading={isPending} sub={data ? `${pct(usedPct, { digits: 1 })} of total margin` : undefined} />
      <Kpi label="Holdings value" paise={data?.holdings_value_paise} loading={isPending} sub={data ? `${data.holdings_count} holdings` : undefined} />
      <Kpi label="Exposure" paise={data?.exposure_paise} loading={isPending} sub={data ? `${data.positions_count} positions · ${data.open_orders_count} open orders` : undefined} />
    </div>
  );
}

const CANCEL_ALL_TEXT = "Cancel all my open orders";

/** Drafts one plan with a CANCEL step per open order in the Copilot. Nothing is cancelled until the trader countersigns it. */
function CancelAllOpenOrders() {
  const open = useOrders("open");
  const redraft = useRedraft();
  const streaming = useChatStore((s) => s.streaming);
  const n = open.data?.length ?? 0;
  if (!n) return null;
  return (
    <Button
      size="sm"
      variant="outline"
      className="h-7 px-2 text-xs"
      disabled={streaming}
      onClick={() => redraft(CANCEL_ALL_TEXT)}
      title={`Drafts "${CANCEL_ALL_TEXT}" in the Copilot: one plan, one CANCEL step per open order (${n}). Nothing is cancelled until you countersign it.`}
    >
      <XCircle /> Cancel all open orders
    </Button>
  );
}

function OrdersPanel() {
  const [filter, setFilter] = useState<OrdersFilter>("open");
  const orders = useOrders(filter);
  const broker = useActiveBroker();
  return (
    <Panel
      title="Orders"
      icon={ListOrdered}
      subtitle={
        broker.kind === "021"
          ? "021 has no client order tag: Countersign matches its orders by their details, and marks orders placed elsewhere"
          : "Every order carries the executor's unique client tag"
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <CancelAllOpenOrders />
          <Tabs value={filter} onValueChange={(v) => setFilter(v as OrdersFilter)}>
          <TabsList className="h-7">
            <TabsTrigger value="open" className="px-2 text-xs">
              Open
            </TabsTrigger>
            <TabsTrigger value="all" className="px-2 text-xs">
              All
            </TabsTrigger>
            <TabsTrigger value="rejected" className="px-2 text-xs">
              Rejected
            </TabsTrigger>
          </TabsList>
          </Tabs>
        </div>
      }
    >
      {orders.isPending ? (
        <SkeletonRows rows={4} />
      ) : orders.error ? (
        <ErrorState error={orders.error} title="Orders unavailable" onRetry={() => void orders.refetch()} className="m-3" />
      ) : orders.data.length === 0 ? (
        <EmptyState
          icon={ListOrdered}
          title={filter === "open" ? "No open orders" : filter === "rejected" ? "No rejected orders" : "No orders today"}
          description="Orders appear here after you countersign and the executor sends them."
        />
      ) : (
        <OrdersTable data={orders.data} maxHeight="420px" />
      )}
    </Panel>
  );
}

export function PortfolioView() {
  const holdings = useHoldings();
  const positions = usePositions();
  return (
    <PageContainer>
      <PageHeader
        icon={Briefcase}
        title="Portfolio"
        description="Read-only view through the broker read gateway. The web app and the AI never hold a write key."
        actions={holdings.dataUpdatedAt ? <span className="num text-xs text-subtle">Updated {time(new Date(holdings.dataUpdatedAt).toISOString())}</span> : null}
      />
      <KpiStrip />
      <Panel title="Holdings" icon={Package} subtitle="Live prices from the tick feed. Rows down more than 5% from average are highlighted.">
        {holdings.isPending ? (
          <SkeletonRows rows={7} />
        ) : holdings.error ? (
          <ErrorState error={holdings.error} title="Holdings unavailable" onRetry={() => void holdings.refetch()} className="m-3" />
        ) : holdings.data.length === 0 ? (
          <EmptyState icon={Package} title="No holdings" description="Delivery holdings appear here once trades settle." />
        ) : (
          <HoldingsTable data={holdings.data} />
        )}
      </Panel>
      <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <Panel title="Positions" icon={Layers} subtitle="Intraday and F&O, net quantity (negative is short)">
          {positions.isPending ? (
            <SkeletonRows rows={3} />
          ) : positions.error ? (
            <ErrorState error={positions.error} title="Positions unavailable" onRetry={() => void positions.refetch()} className="m-3" />
          ) : positions.data.length === 0 ? (
            <EmptyState icon={Layers} title="No open positions" />
          ) : (
            <PositionsTable data={positions.data} />
          )}
        </Panel>
        <OrdersPanel />
      </div>
    </PageContainer>
  );
}
