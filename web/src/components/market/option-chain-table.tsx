"use client";

import { Fragment } from "react";
import { compactCount, pct, price, toneClass } from "@/lib/format";
import type { OptionChain, OptionChainRow } from "@/lib/types";
import { cn } from "@/lib/utils";

/** NIFTY option chain: calls left, strike centre, puts right. ATM highlighted, ITM shaded by distance. */
export function OptionChainTable({ chain, compact }: { chain: OptionChain; compact?: boolean }) {
  const spot = chain.spot_paise;
  const maxOi = Math.max(1, ...chain.rows.flatMap((r) => [r.ce.oi, r.pe.oi]));
  const spotAfter = (r: OptionChainRow, next: OptionChainRow | undefined) =>
    r.strike_paise <= spot && (!next || next.strike_paise > spot);

  const th = "sticky top-0 z-10 h-8 bg-surface-1 px-2 text-[10px] font-semibold tracking-[0.07em] text-muted-foreground uppercase border-b border-hairline";
  const td = "h-9 px-2 num text-[12.5px] whitespace-nowrap border-b border-hairline";

  return (
    <div className="scrollbar-thin relative w-full overflow-auto">
      <table className="w-full border-separate border-spacing-0">
        <thead>
          <tr>
            <th colSpan={compact ? 3 : 4} className={cn(th, "text-center text-profit/90")}>
              Calls (CE)
            </th>
            <th className={cn(th, "text-center")}>Strike</th>
            <th colSpan={compact ? 3 : 4} className={cn(th, "text-center text-loss/90")}>
              Puts (PE)
            </th>
          </tr>
          <tr>
            <th className={cn(th, "top-8 text-right")}>OI</th>
            {!compact && <th className={cn(th, "top-8 text-right")}>IV</th>}
            <th className={cn(th, "top-8 text-right")}>Chg</th>
            <th className={cn(th, "top-8 text-right")}>LTP</th>
            <th className={cn(th, "top-8 text-center")}>{(chain.strike_step_paise / 100).toFixed(0)} pt</th>
            <th className={cn(th, "top-8 text-left")}>LTP</th>
            <th className={cn(th, "top-8 text-left")}>Chg</th>
            {!compact && <th className={cn(th, "top-8 text-left")}>IV</th>}
            <th className={cn(th, "top-8 text-left")}>OI</th>
          </tr>
        </thead>
        <tbody>
          {chain.rows.map((r, i) => {
            const ceItm = r.strike_paise < spot;
            const peItm = r.strike_paise > spot;
            const near = Math.abs(r.distance);
            const shade = (itm: boolean) =>
              itm ? (near <= 2 ? "bg-warning/[0.07]" : near <= 4 ? "bg-warning/[0.045]" : "bg-warning/[0.025]") : "";
            const dim = near > 4 ? "text-muted-foreground" : "";
            return (
              <Fragment key={r.strike_paise}>
                <tr className={cn("group/row", r.is_atm && "[&>td]:bg-human/[0.09]")}>
                  <td className={cn(td, "relative text-right", shade(ceItm), dim)}>
                    <span
                      className="absolute right-0 bottom-1 h-[3px] rounded-l-full bg-loss/45"
                      style={{ width: `${(r.ce.oi / maxOi) * 100}%` }}
                      aria-hidden
                    />
                    <span className="relative">{compactCount(r.ce.oi)}</span>
                  </td>
                  {!compact && <td className={cn(td, "text-right text-muted-foreground", shade(ceItm))}>{pct(r.ce.iv_bps, { digits: 1 })}</td>}
                  <td className={cn(td, "text-right", shade(ceItm), toneClass(r.ce.change_bps))}>{pct(r.ce.change_bps, { sign: true, digits: 1 })}</td>
                  <td className={cn(td, "text-right font-medium", shade(ceItm))}>{price(r.ce.ltp_paise)}</td>
                  <td
                    className={cn(
                      td,
                      "bg-surface-2 text-center font-semibold",
                      r.is_atm && "bg-human/15 text-human",
                    )}
                  >
                    <span className="inline-flex items-center gap-1.5">
                      {(r.strike_paise / 100).toLocaleString("en-IN")}
                      {r.is_atm && (
                        <span className="rounded bg-human px-1 text-[9px] font-bold tracking-wider text-background">ATM</span>
                      )}
                    </span>
                  </td>
                  <td className={cn(td, "text-left font-medium", shade(peItm))}>{price(r.pe.ltp_paise)}</td>
                  <td className={cn(td, "text-left", shade(peItm), toneClass(r.pe.change_bps))}>{pct(r.pe.change_bps, { sign: true, digits: 1 })}</td>
                  {!compact && <td className={cn(td, "text-left text-muted-foreground", shade(peItm))}>{pct(r.pe.iv_bps, { digits: 1 })}</td>}
                  <td className={cn(td, "relative text-left", shade(peItm), dim)}>
                    <span
                      className="absolute bottom-1 left-0 h-[3px] rounded-r-full bg-profit/45"
                      style={{ width: `${(r.pe.oi / maxOi) * 100}%` }}
                      aria-hidden
                    />
                    <span className="relative">{compactCount(r.pe.oi)}</span>
                  </td>
                </tr>
                {spotAfter(r, chain.rows[i + 1]) && (
                  <tr aria-label={`Spot ${price(spot)}`}>
                    <td colSpan={compact ? 7 : 9} className="h-0 p-0">
                      <div className="relative h-0">
                        <div className="absolute inset-x-0 -top-px h-px bg-info/70" />
                        <span className="num absolute left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-info px-2 py-0.5 text-[10px] font-semibold text-background">
                          NIFTY {price(spot)}
                        </span>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
