"use client";

import { useMemo, useState } from "react";
import { TrendingDown } from "lucide-react";
import { Chip, LookChip, SideBadge } from "@/components/ui/chip";
import { InstrumentCell, SortTh, TableShell, Td, Th } from "@/components/ui/data-table";
import { flashClass } from "@/components/ui/live-price";
import { inr, pct, pnl, price, qty, time, toneClass } from "@/lib/format";
import { ORDER_STATUS } from "@/lib/status";
import { useTick, useTickMeta } from "@/lib/store";
import type { BrokerOrder, Holding, Position } from "@/lib/types";
import { cn } from "@/lib/utils";

const LOSER_BPS = -500;

function LtpCell({ instrumentId, fallback }: { instrumentId: string; fallback: number }) {
  const tick = useTick(instrumentId);
  const meta = useTickMeta(instrumentId);
  return (
    <span className={cn("num inline-block rounded-[4px] px-1 -mx-1", flashClass(meta?.dir, meta?.n))}>
      {price(tick?.ltp_paise ?? fallback)}
    </span>
  );
}

function HoldingRow({ h, compact }: { h: Holding; compact?: boolean }) {
  const tick = useTick(h.instrument.instrument_id);
  const ltp = tick?.ltp_paise ?? h.ltp_paise;
  const pnlPaise = (ltp - h.avg_price_paise) * h.quantity;
  const pnlBps = Math.round(((ltp - h.avg_price_paise) / h.avg_price_paise) * 10000);
  const dayBps = tick?.change_bps ?? h.day_change_bps;
  const loser = pnlBps <= LOSER_BPS;
  return (
    <tr className={cn("transition-colors hover:bg-foreground/[0.03]", loser && "bg-loss/[0.06] hover:bg-loss/[0.09]")}>
      <Td className={cn(loser && "shadow-[inset_2px_0_0_var(--loss)]")}>
        <InstrumentCell
          symbol={h.instrument.symbol}
          name={compact ? undefined : h.instrument.display_name}
          meta={
            loser ? (
              <Chip tone="loss" size="xs" icon={TrendingDown}>
                {"Down >5%"}
              </Chip>
            ) : null
          }
        />
      </Td>
      <Td align="right" num>
        {qty(h.quantity)}
      </Td>
      <Td align="right" num className="text-muted-foreground">
        {price(h.avg_price_paise)}
      </Td>
      <Td align="right">
        <LtpCell instrumentId={h.instrument.instrument_id} fallback={h.ltp_paise} />
      </Td>
      {!compact && (
        <Td align="right" num>
          {inr(ltp * h.quantity)}
        </Td>
      )}
      <Td align="right" num className={toneClass(pnlPaise)}>
        <div className="flex flex-col items-end leading-tight">
          <span>{pnl(pnlPaise)}</span>
          <span className="text-[11px] opacity-80">{pct(pnlBps, { sign: true })}</span>
        </div>
      </Td>
      <Td align="right" num className={toneClass(dayBps)}>
        {pct(dayBps, { sign: true })}
      </Td>
    </tr>
  );
}

type HoldingSort = "symbol" | "value" | "pnl" | "day";

export function HoldingsTable({ data, compact, sortable = true, maxHeight }: { data: Holding[]; compact?: boolean; sortable?: boolean; maxHeight?: string }) {
  const [sort, setSort] = useState<{ key: HoldingSort; dir: "asc" | "desc" }>({ key: "value", dir: "desc" });
  const rows = useMemo(() => {
    if (!sortable) return data;
    const val = (h: Holding) =>
      sort.key === "symbol" ? h.instrument.symbol : sort.key === "value" ? h.value_paise : sort.key === "pnl" ? h.pnl_bps : h.day_change_bps;
    return [...data].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const c = typeof va === "string" ? va.localeCompare(vb as string) : (va as number) - (vb as number);
      return sort.dir === "asc" ? c : -c;
    });
  }, [data, sort, sortable]);
  const onSort = (k: HoldingSort) => setSort((s) => (s.key === k ? { key: k, dir: s.dir === "asc" ? "desc" : "asc" } : { key: k, dir: k === "symbol" ? "asc" : "desc" }));
  return (
    <TableShell maxHeight={maxHeight}>
      <thead>
        <tr>
          {sortable ? <SortTh label="Instrument" k="symbol" sort={sort} onSort={onSort} /> : <Th>Instrument</Th>}
          <Th align="right">Qty</Th>
          <Th align="right">Avg</Th>
          <Th align="right">LTP</Th>
          {!compact && (sortable ? <SortTh label="Value" k="value" sort={sort} onSort={onSort} align="right" /> : <Th align="right">Value</Th>)}
          {sortable ? <SortTh label="P&L" k="pnl" sort={sort} onSort={onSort} align="right" /> : <Th align="right">P&L</Th>}
          {sortable ? <SortTh label="Day" k="day" sort={sort} onSort={onSort} align="right" /> : <Th align="right">Day</Th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((h) => (
          <HoldingRow key={h.instrument.instrument_id} h={h} compact={compact} />
        ))}
      </tbody>
    </TableShell>
  );
}

function PositionRow({ p }: { p: Position }) {
  const tick = useTick(p.instrument.instrument_id);
  const ltp = tick?.ltp_paise ?? p.ltp_paise;
  const pnlPaise = (ltp - p.avg_price_paise) * p.quantity;
  return (
    <tr className="hover:bg-foreground/[0.03]">
      <Td>
        <InstrumentCell
          symbol={p.instrument.segment === "FO" ? p.instrument.display_name : p.instrument.symbol}
          name={p.instrument.segment === "FO" ? p.instrument.symbol : p.instrument.display_name}
        />
      </Td>
      <Td>
        <Chip tone="neutral" size="xs">
          {p.product}
        </Chip>
      </Td>
      <Td align="right" num className={p.quantity < 0 ? "text-loss" : undefined}>
        {qty(p.quantity, { sign: true })}
      </Td>
      <Td align="right" num className="text-muted-foreground">
        {price(p.avg_price_paise)}
      </Td>
      <Td align="right">
        <LtpCell instrumentId={p.instrument.instrument_id} fallback={p.ltp_paise} />
      </Td>
      <Td align="right" num className={toneClass(pnlPaise)}>
        {pnl(pnlPaise)}
      </Td>
      <Td align="right" num className={toneClass(p.day_pnl_paise)}>
        {pnl(p.day_pnl_paise)}
      </Td>
    </tr>
  );
}

export function PositionsTable({ data, maxHeight }: { data: Position[]; maxHeight?: string }) {
  return (
    <TableShell maxHeight={maxHeight}>
      <thead>
        <tr>
          <Th>Instrument</Th>
          <Th>Product</Th>
          <Th align="right">Net qty</Th>
          <Th align="right">Avg</Th>
          <Th align="right">LTP</Th>
          <Th align="right">P&L</Th>
          <Th align="right">Day P&L</Th>
        </tr>
      </thead>
      <tbody>
        {data.map((p) => (
          <PositionRow key={`${p.instrument.instrument_id}-${p.product}`} p={p} />
        ))}
      </tbody>
    </TableShell>
  );
}

export function OrdersTable({ data, maxHeight, compact }: { data: BrokerOrder[]; maxHeight?: string; compact?: boolean }) {
  return (
    <TableShell maxHeight={maxHeight}>
      <thead>
        <tr>
          <Th>Time</Th>
          <Th>Side</Th>
          <Th>Instrument</Th>
          <Th align="right">Qty</Th>
          <Th align="right">Price</Th>
          <Th>Status</Th>
          {!compact && <Th>Client tag</Th>}
        </tr>
      </thead>
      <tbody>
        {data.map((o) => {
          const look = ORDER_STATUS[o.status];
          const px = o.avg_fill_price_paise ?? o.limit_price_paise ?? o.protection_price_paise;
          return (
            <tr key={o.broker_order_id} className="align-top hover:bg-foreground/[0.03]">
              <Td num className="text-muted-foreground">
                {time(o.placed_at)}
              </Td>
              <Td>
                <SideBadge side={o.side} size="sm" />
              </Td>
              <Td>
                <InstrumentCell
                  symbol={o.instrument.segment === "FO" ? o.instrument.display_name : o.instrument.symbol}
                  name={`${o.order_type} · ${o.product} · ${o.validity}`}
                />
              </Td>
              <Td align="right" num>
                {o.filled_quantity > 0 && o.filled_quantity < o.quantity ? (
                  <span>
                    {qty(o.filled_quantity)}
                    <span className="unit">/{qty(o.quantity)}</span>
                  </span>
                ) : (
                  qty(o.quantity)
                )}
              </Td>
              <Td align="right" num>
                {price(px)}
                {o.avg_fill_price_paise && <div className="text-[10px] text-subtle">avg fill</div>}
              </Td>
              <Td className="whitespace-normal">
                <LookChip look={look} size="xs" />
                {o.reject_reason && <p className="mt-1 max-w-[280px] text-[11px] leading-snug text-loss">{o.reject_reason}</p>}
              </Td>
              {!compact && (
                <Td className="font-mono text-[11px] text-muted-foreground">
                  {o.client_order_id ?? "-"}
                  <div className="text-[10px] text-subtle">{o.broker_order_id}</div>
                </Td>
              )}
            </tr>
          );
        })}
      </tbody>
    </TableShell>
  );
}
