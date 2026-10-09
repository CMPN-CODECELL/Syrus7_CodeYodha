"use client";

import { ArrowRight, ChevronDown } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { LivePrice } from "@/components/ui/live-price";
import { inr, ms, price, qty } from "@/lib/format";
import type { OrderPreview } from "@/lib/types";
import { cn } from "@/lib/utils";

function Row({ label, children, strong, className }: { label: string; children: React.ReactNode; strong?: boolean; className?: string }) {
  return (
    <div className={cn("flex min-h-7 items-center justify-between gap-3", className)}>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn("num text-right text-[13px]", strong && "text-[15px] font-semibold")}>{children}</span>
    </div>
  );
}

function FromTo({ from, to, fmt, warnIfNegative }: { from: number; to: number; fmt: (n: number) => string; warnIfNegative?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-muted-foreground">{fmt(from)}</span>
      <ArrowRight className="size-3 text-subtle" aria-hidden />
      <span className={cn("font-semibold", warnIfNegative && to < 0 && "text-danger")}>{fmt(to)}</span>
    </span>
  );
}

/** Wallet-style money preview: what this order costs and what it changes. */
export function WalletPreview({ preview, instrumentId, side }: { preview: OrderPreview; instrumentId: string; side: "BUY" | "SELL" }) {
  return (
    <div className="flex flex-col rounded-lg bg-surface-2/60 p-3 ring-1 ring-hairline">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[10.5px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">Preview</span>
        <span className="text-[11px] text-subtle">
          {preview.session.label} · tick {ms(preview.tick_age_ms)} old
        </span>
      </div>
      <Row label="Current quote (live)">
        <LivePrice instrumentId={instrumentId} fallback={preview.reference_price_paise} symbol />
      </Row>
      <Row label={side === "BUY" ? "Worst-case price (max you pay)" : "Worst-case price (min you get)"}>{inr(preview.worst_case_price_paise)}</Row>
      <div className="my-1.5 h-px bg-hairline" />
      <Row label="Order value" strong>
        {inr(preview.order_value_paise)}
      </Row>
      <Row label="Estimated margin">{inr(preview.estimated_margin_paise)}</Row>
      <Collapsible>
        <CollapsibleTrigger className="group/charges flex min-h-7 w-full items-center justify-between gap-3 rounded text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            Estimated charges
            <ChevronDown className="size-3 transition-transform group-data-[panel-open]/charges:rotate-180" aria-hidden />
          </span>
          <span className="num text-[13px]">{inr(preview.estimated_charges_paise)}</span>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="mt-1 mb-1 space-y-0.5 rounded-md bg-background/40 px-2.5 py-1.5 ring-1 ring-hairline">
            {preview.charges_breakdown.map((c) => (
              <div key={c.label} className="flex justify-between text-[11.5px]">
                <span className="text-muted-foreground">{c.label}</span>
                <span className="num">{inr(c.paise)}</span>
              </div>
            ))}
          </div>
        </CollapsibleContent>
      </Collapsible>
      <div className="my-1.5 h-px bg-hairline" />
      <Row label="Position">
        <FromTo from={preview.current_position_qty} to={preview.resulting_position_qty} fmt={(n) => qty(n)} />
      </Row>
      <Row label="Available margin">
        <FromTo from={preview.available_margin_paise} to={preview.margin_after_paise} fmt={(n) => inr(n)} warnIfNegative />
      </Row>
      <Row label="Exposure after">{inr(preview.exposure_after_paise)}</Row>
      <p className="mt-1.5 text-[11px] text-subtle">
        Band {price(preview.band_low_paise)} to {price(preview.band_high_paise)} (±{(preview.band_bps / 100).toFixed(2)}%) from the live
        reference.
      </p>
    </div>
  );
}
