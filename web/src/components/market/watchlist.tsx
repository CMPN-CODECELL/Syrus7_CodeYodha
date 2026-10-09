"use client";

import { LiveChange, LivePrice } from "@/components/ui/live-price";
import { SkeletonRows, ErrorState } from "@/components/ui/states";
import { useWatchlist } from "@/lib/queries";
import { useTick, useUiStore } from "@/lib/store";
import type { Quote } from "@/lib/types";
import { cn } from "@/lib/utils";

function ChangeBar({ instrumentId, fallbackBps }: { instrumentId: string; fallbackBps: number }) {
  const tick = useTick(instrumentId);
  const bps = tick?.change_bps ?? fallbackBps;
  const w = Math.min(100, (Math.abs(bps) / 300) * 100);
  return (
    <span className="relative h-1 w-12 overflow-hidden rounded-full bg-foreground/[0.06]" aria-hidden>
      <span
        className={cn("absolute inset-y-0 rounded-full", bps >= 0 ? "left-1/2 bg-profit/70" : "right-1/2 bg-loss/70")}
        style={{ width: `${w / 2}%` }}
      />
    </span>
  );
}

function WatchRow({ q, selected, onSelect, dense }: { q: Quote; selected: boolean; onSelect: () => void; dense?: boolean }) {
  const id = q.instrument.instrument_id;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "group flex w-full items-center gap-3 border-b border-hairline px-3 text-left transition-colors outline-none focus-visible:bg-foreground/[0.05]",
        dense ? "min-h-11" : "min-h-12",
        selected ? "bg-human/[0.08] shadow-[inset_2px_0_0_var(--human)]" : "hover:bg-foreground/[0.03]",
      )}
    >
      <span className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="flex items-center gap-1.5 text-[13px] font-medium">
          {q.instrument.symbol}
          {q.instrument.segment === "IDX" && <span className="rounded bg-foreground/[0.07] px-1 text-[9px] text-muted-foreground">INDEX</span>}
        </span>
        {!dense && <span className="truncate text-[11px] text-subtle">{q.instrument.display_name}</span>}
      </span>
      <ChangeBar instrumentId={id} fallbackBps={q.tick.change_bps} />
      <span className="flex w-24 flex-col items-end leading-tight">
        <LivePrice instrumentId={id} fallback={q.tick.ltp_paise} className="text-[13px] font-medium" />
        <LiveChange instrumentId={id} fallbackBps={q.tick.change_bps} className="text-[11px]" />
      </span>
    </button>
  );
}

export function Watchlist({ dense, className }: { dense?: boolean; className?: string }) {
  const { data, error, isPending, refetch } = useWatchlist();
  const selected = useUiStore((s) => s.marketSymbol);
  const setSelected = useUiStore((s) => s.setMarketSymbol);
  if (isPending) return <SkeletonRows rows={7} />;
  if (error) return <ErrorState error={error} title="Watchlist unavailable" onRetry={() => void refetch()} className="m-3" compact />;
  return (
    <div className={cn("flex flex-col", className)} role="list" aria-label="Watchlist">
      {data.map((q) => (
        <WatchRow key={q.instrument.instrument_id} q={q} dense={dense} selected={q.instrument.symbol === selected} onSelect={() => setSelected(q.instrument.symbol)} />
      ))}
    </div>
  );
}
