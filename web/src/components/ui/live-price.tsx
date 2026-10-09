"use client";

import { price, inr, pct, toneClass } from "@/lib/format";
import { useTick, useTickMeta } from "@/lib/store";
import { cn } from "@/lib/utils";

/** Flash class for a tick: swaps between two identical keyframes so each tick restarts it. */
export function flashClass(dir: 1 | -1 | 0 | undefined, n: number | undefined): string {
  if (!dir || n === undefined) return "";
  const k = n % 2;
  return dir > 0 ? (k ? "animate-flash-up-1" : "animate-flash-up-0") : k ? "animate-flash-down-1" : "animate-flash-down-0";
}

/** Live LTP for an instrument; flashes green/red for ~600 ms on each tick without remounting. */
export function LivePrice({
  instrumentId,
  fallback,
  className,
  symbol = false,
}: {
  instrumentId: string | null | undefined;
  fallback?: number | null;
  className?: string;
  symbol?: boolean;
}) {
  const tick = useTick(instrumentId);
  const meta = useTickMeta(instrumentId);
  const ltp = tick?.ltp_paise ?? fallback ?? null;
  return (
    <span
      className={cn(
        "num inline-block rounded-[4px] px-1 -mx-1 tabular-nums",
        flashClass(meta?.dir, meta?.n),
        tick?.stale && "text-muted-foreground line-through decoration-warning/60",
        className,
      )}
      title={tick?.stale ? "Stale: last tick is older than 5 s" : undefined}
    >
      {symbol ? inr(ltp) : price(ltp)}
    </span>
  );
}

/** Live day change percent for an instrument (from the tick), colored. */
export function LiveChange({
  instrumentId,
  fallbackBps,
  className,
}: {
  instrumentId: string | null | undefined;
  fallbackBps?: number | null;
  className?: string;
}) {
  const tick = useTick(instrumentId);
  const bps = tick?.change_bps ?? fallbackBps ?? null;
  return <span className={cn("num tabular-nums", toneClass(bps), className)}>{pct(bps, { sign: true })}</span>;
}
