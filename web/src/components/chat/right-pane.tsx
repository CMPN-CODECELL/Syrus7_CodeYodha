"use client";

import { Activity, Briefcase, ChartCandlestick } from "lucide-react";
import Link from "next/link";
import { PriceChart } from "@/components/market/price-chart";
import { Watchlist } from "@/components/market/watchlist";
import { OutcomeFeed } from "@/components/outcomes/outcome-feed";
import { MarketSourceBadge } from "@/components/shell/status-indicators";
import { Kpi } from "@/components/ui/kpi";
import { LivePrice } from "@/components/ui/live-price";
import { ErrorState, SkeletonRows } from "@/components/ui/states";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { pct, qty, toneClass } from "@/lib/format";
import { useAccountSummary, useHoldings } from "@/lib/queries";
import { useLiveStore, useTick, useUiStore, type CopilotTab } from "@/lib/store";
import type { Holding } from "@/lib/types";
import { cn } from "@/lib/utils";

function MiniHolding({ h }: { h: Holding }) {
  const tick = useTick(h.instrument.instrument_id);
  const ltp = tick?.ltp_paise ?? h.ltp_paise;
  const bps = Math.round(((ltp - h.avg_price_paise) / h.avg_price_paise) * 10000);
  return (
    <li className={cn("flex min-h-10 items-center gap-3 px-3", bps <= -500 && "bg-loss/[0.06]")}>
      <span className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="text-[13px] font-medium">{h.instrument.symbol}</span>
        <span className="num text-[11px] text-subtle">{qty(h.quantity)} shares</span>
      </span>
      <LivePrice instrumentId={h.instrument.instrument_id} fallback={h.ltp_paise} className="text-[13px]" />
      <span className={cn("num w-16 text-right text-xs", toneClass(bps))}>{pct(bps, { sign: true })}</span>
    </li>
  );
}

function AccountTab() {
  const sum = useAccountSummary();
  const holdings = useHoldings();
  return (
    <div className="space-y-3 p-3">
      {sum.error ? (
        <ErrorState error={sum.error} title="Account summary unavailable" onRetry={() => void sum.refetch()} compact />
      ) : (
        <div className="grid grid-cols-2 gap-2">
          <Kpi label="Day P&L" paise={sum.data?.day_pnl_paise} signed loading={sum.isPending} />
          <Kpi label="Total P&L" paise={sum.data?.total_pnl_paise} signed loading={sum.isPending} />
          <Kpi label="Available margin" paise={sum.data?.available_margin_paise} loading={sum.isPending} />
          <Kpi label="Holdings value" paise={sum.data?.holdings_value_paise} loading={sum.isPending} />
        </div>
      )}
      <div className="overflow-hidden rounded-lg ring-1 ring-hairline">
        <div className="flex h-8 items-center justify-between border-b border-hairline bg-surface-1 px-3 text-[10.5px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
          Holdings
          <Link href="/portfolio" className="tracking-normal normal-case hover:text-foreground">
            All
          </Link>
        </div>
        {holdings.isPending ? (
          <SkeletonRows rows={5} />
        ) : holdings.error ? (
          <ErrorState error={holdings.error} title="Holdings unavailable" onRetry={() => void holdings.refetch()} compact className="m-2" />
        ) : (
          <ul className="divide-y divide-hairline bg-surface-1">
            {holdings.data.map((h) => (
              <MiniHolding key={h.instrument.instrument_id} h={h} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function MarketTab() {
  const symbol = useUiStore((s) => s.marketSymbol);
  const id = useLiveStore((s) => s.bySymbol[symbol]);
  return (
    <div className="flex flex-col">
      <div className="border-b border-hairline p-3">
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-sm font-semibold">{symbol}</span>
          <MarketSourceBadge className="mr-auto h-5 px-2 text-[10px]" />
          <LivePrice instrumentId={id} className="text-sm font-semibold" />
        </div>
        <PriceChart key={symbol} symbol={symbol} instrumentId={id} height={180} />
      </div>
      <Watchlist dense />
    </div>
  );
}

function OutcomesTab() {
  const outcomes = useLiveStore((s) => s.outcomes);
  return <OutcomeFeed items={outcomes.slice(0, 60)} dense />;
}

export function RightPane({ className }: { className?: string }) {
  const tab = useUiStore((s) => s.copilotTab);
  const setTab = useUiStore((s) => s.setCopilotTab);
  const unread = useLiveStore((s) => s.outcomes.length);
  return (
    <Tabs value={tab} onValueChange={(v) => setTab(v as CopilotTab)} className={cn("flex h-full min-h-0 flex-col gap-0", className)}>
      <div className="shrink-0 border-b border-hairline px-3 py-2">
        <TabsList className="w-full">
          <TabsTrigger value="account" className="text-xs">
            <Briefcase /> Account
          </TabsTrigger>
          <TabsTrigger value="market" className="text-xs">
            <ChartCandlestick /> Market
          </TabsTrigger>
          <TabsTrigger value="outcomes" className="text-xs">
            <Activity /> Live
            {unread > 0 && <span className="num rounded bg-human/15 px-1 text-[10px] text-human">{Math.min(unread, 99)}</span>}
          </TabsTrigger>
        </TabsList>
      </div>
      <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto">
        <TabsContent value="account">
          <AccountTab />
        </TabsContent>
        <TabsContent value="market">
          <MarketTab />
        </TabsContent>
        <TabsContent value="outcomes">
          <OutcomesTab />
        </TabsContent>
      </div>
    </Tabs>
  );
}
