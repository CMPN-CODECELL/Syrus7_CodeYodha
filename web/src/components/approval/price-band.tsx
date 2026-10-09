"use client";

import { TriangleAlert } from "lucide-react";
import { price } from "@/lib/format";
import { useTick } from "@/lib/store";
import type { OrderPreview } from "@/lib/types";
import { cn } from "@/lib/utils";

export function useBandState(preview: OrderPreview | null | undefined, instrumentId: string | undefined) {
  const tick = useTick(instrumentId);
  if (!preview) return { ltp: null, outside: false, stale: false };
  const ltp = tick?.ltp_paise ?? preview.reference_price_paise;
  const outside = ltp < preview.band_low_paise || ltp > preview.band_high_paise;
  return { ltp, outside, stale: !!tick?.stale };
}

/** Band meter: band_low ... reference ... band_high with a live LTP marker from SSE ticks. */
export function PriceBandMeter({
  preview,
  instrumentId,
  active = true,
  className,
}: {
  preview: OrderPreview;
  instrumentId: string;
  /** When false (already executed), the band is shown for reference without the void warning. */
  active?: boolean;
  className?: string;
}) {
  const { ltp, outside, stale } = useBandState(preview, instrumentId);
  const lo = preview.band_low_paise;
  const hi = preview.band_high_paise;
  const pad = (hi - lo) * 0.45;
  const min = Math.min(lo - pad, ltp ?? lo);
  const max = Math.max(hi + pad, ltp ?? hi);
  const pos = (v: number) => `${((v - min) / (max - min)) * 100}%`;
  const showVoid = active && outside;

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex items-center justify-between text-[10.5px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
        <span>Approved price band</span>
        <span className={cn("num normal-case tracking-normal", showVoid ? "text-danger" : "text-muted-foreground")}>
          LTP {price(ltp)}
          {stale && <span className="ml-1 text-warning">(stale)</span>}
        </span>
      </div>
      <div className="relative h-9 select-none" role="img" aria-label={`Approved band ${price(lo)} to ${price(hi)}, live price ${price(ltp)}${showVoid ? ", outside the band" : ""}`}>
        <div className="absolute inset-x-0 top-4 h-1 rounded-full bg-foreground/[0.07]" />
        <div
          className={cn(
            "absolute top-[11px] h-[14px] rounded-[4px] ring-1 ring-inset",
            showVoid ? "bg-danger/10 ring-danger/40" : "bg-human/15 ring-human/40",
          )}
          style={{ left: pos(lo), width: `calc(${pos(hi)} - ${pos(lo)})` }}
        />
        <div className="absolute top-[7px] h-[22px] w-px border-l border-dashed border-foreground/40" style={{ left: pos(preview.reference_price_paise) }} />
        {ltp !== null && (
          <div className="absolute top-0 -translate-x-1/2 transition-[left] duration-300 ease-out" style={{ left: pos(ltp) }}>
            <div className={cn("mx-auto h-9 w-[3px] rounded-full", showVoid ? "bg-danger" : "bg-foreground")} />
          </div>
        )}
      </div>
      <div className="relative h-4 text-[11px]">
        <span className="num absolute -translate-x-1/2 text-muted-foreground" style={{ left: pos(lo) }}>
          {price(lo)}
        </span>
        <span className="num absolute -translate-x-1/2 text-subtle" style={{ left: pos(preview.reference_price_paise) }}>
          ref
        </span>
        <span className="num absolute -translate-x-1/2 text-muted-foreground" style={{ left: pos(hi) }}>
          {price(hi)}
        </span>
      </div>
      {showVoid && (
        <p role="alert" className="flex items-start gap-2 rounded-md bg-danger/10 px-2.5 py-2 text-xs font-medium text-danger ring-1 ring-danger/30">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          Price left your approved band. This approval is void; review again.
        </p>
      )}
    </div>
  );
}
