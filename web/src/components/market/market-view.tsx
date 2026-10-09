"use client";

import { useState } from "react";
import { ChartCandlestick, Grid3x3, List } from "lucide-react";
import { MarketSourceBadge, useMarketSource } from "@/components/shell/status-indicators";
import { LiveChange, LivePrice } from "@/components/ui/live-price";
import { PageContainer, PageHeader, Panel } from "@/components/ui/panel";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ErrorState, SkeletonRows } from "@/components/ui/states";
import { compactCount, date, price } from "@/lib/format";
import { useMandates, useOptionChain, useProposals } from "@/lib/queries";
import { useLiveStore, useUiStore } from "@/lib/store";
import { OptionChainTable } from "./option-chain-table";
import { PriceChart, type ChartLine } from "./price-chart";
import { Watchlist } from "./watchlist";

function ChartPanel() {
  const symbol = useUiStore((s) => s.marketSymbol);
  const id = useLiveStore((s) => s.bySymbol[symbol]);
  const tick = useLiveStore((s) => (id ? s.ticks[id] : undefined));
  const mandates = useMandates();
  const awaiting = useProposals({ status: "AWAITING_APPROVAL" });
  const lines: ChartLine[] = [];
  for (const m of mandates.data ?? []) {
    if (m.instrument.symbol === symbol && m.state === "ACTIVE" && m.predicate.field === "LTP") {
      lines.push({ price: m.predicate.threshold / 100, title: `Mandate ${m.predicate.op === "LTE" ? "<=" : ">="}`, tone: "warning" });
    }
  }
  // Only the newest proposal awaiting your signature on this symbol, to keep the chart readable.
  const newest = [...(awaiting.data ?? [])]
    .filter((p) => p.kind !== "MANDATE" && p.steps.some((st) => st.canonical.instrument.symbol === symbol && st.preview))
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
  const band = newest?.steps.find((st) => st.canonical.instrument.symbol === symbol && st.preview)?.preview;
  if (newest && band) {
    lines.push({ price: band.band_low_paise / 100, title: `Band low ${newest.short_hash.slice(0, 6)}`, tone: "human" });
    lines.push({ price: band.band_high_paise / 100, title: `Band high ${newest.short_hash.slice(0, 6)}`, tone: "human" });
  }
  return (
    <Panel
      title={
        <span className="flex items-baseline gap-3">
          <span className="text-base">{symbol}</span>
          <LivePrice instrumentId={id} className="text-base font-semibold" />
          <LiveChange instrumentId={id} className="text-xs" />
        </span>
      }
      actions={
        tick && (
          <span className="num hidden gap-3 text-[11px] text-muted-foreground sm:flex">
            <span>Bid {price(tick.bid_paise)}</span>
            <span>Ask {price(tick.ask_paise)}</span>
            {tick.volume > 0 && <span>Vol {compactCount(tick.volume)}</span>}
          </span>
        )
      }
      bodyClassName="p-2"
    >
      <PriceChart key={symbol} symbol={symbol} instrumentId={id} height={340} lines={lines} />
      {lines.length > 0 && (
        <p className="px-2 pt-1 text-[11px] text-subtle">
          Dashed lines: armed mandate triggers (amber) and the approval band of the newest order awaiting your countersignature (emerald).
        </p>
      )}
    </Panel>
  );
}

function OptionChainPanel() {
  const [expiry, setExpiry] = useState<string | undefined>(undefined);
  const { data, error, isPending, refetch } = useOptionChain(expiry);
  const items = (data?.expiries ?? []).map((e) => ({ value: e, label: date(e) }));
  return (
    <Panel
      title="NIFTY option chain"
      icon={Grid3x3}
      subtitle={data ? `Spot ${price(data.spot_paise)} · ATM ${(data.atm_strike_paise / 100).toLocaleString("en-IN")} · lot ${data.lot_size} · ITM shaded` : "Calls left, puts right"}
      actions={
        data && (
          <Select items={items} value={expiry ?? data.expiry} onValueChange={(v) => v && setExpiry(String(v))}>
            <SelectTrigger size="sm" className="w-36" aria-label="Expiry">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {items.map((e) => (
                <SelectItem key={e.value} value={e.value}>
                  {e.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )
      }
    >
      {isPending ? (
        <SkeletonRows rows={9} />
      ) : error ? (
        <ErrorState error={error} title="Option chain unavailable" onRetry={() => void refetch()} className="m-3" />
      ) : (
        <div className="max-h-[560px] overflow-auto">
          <OptionChainTable chain={data} />
        </div>
      )}
    </Panel>
  );
}

export function MarketView() {
  const source = useMarketSource();
  return (
    <PageContainer wide>
      <PageHeader
        icon={ChartCandlestick}
        title="Market"
        description={`${source.title}. Ticks arrive over one shared connection; prices flash on every tick and stale data is struck through.`}
        actions={<MarketSourceBadge />}
      />
      <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
        <Panel title="Watchlist" icon={List} className="lg:sticky lg:top-0 lg:self-start" bodyClassName="max-h-[70dvh] overflow-y-auto scrollbar-thin">
          <Watchlist />
        </Panel>
        <div className="min-w-0 space-y-4">
          <ChartPanel />
          <OptionChainPanel />
        </div>
      </div>
    </PageContainer>
  );
}
