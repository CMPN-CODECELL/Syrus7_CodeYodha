"use client";

import NumberFlow from "@number-flow/react";
import type { ReactNode } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const INR_FORMAT = {
  style: "currency",
  currency: "INR",
  currencyDisplay: "narrowSymbol",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
} as const;

/** Animated INR amount from integer paise (en-IN grouping: ₹1,23,45,678.50). */
export function Money({
  paise,
  signed,
  className,
  decimals = true,
}: {
  paise: number;
  signed?: boolean;
  className?: string;
  decimals?: boolean;
}) {
  return (
    <NumberFlow
      className={cn("num", className)}
      value={paise / 100}
      locales="en-IN"
      format={{
        ...INR_FORMAT,
        minimumFractionDigits: decimals ? 2 : 0,
        maximumFractionDigits: decimals ? 2 : 0,
        signDisplay: signed ? "exceptZero" : "auto",
      }}
    />
  );
}

export function Kpi({
  label,
  paise,
  signed,
  sub,
  loading,
  className,
  emphasis,
  tone,
}: {
  label: string;
  paise: number | undefined;
  signed?: boolean;
  sub?: ReactNode;
  loading?: boolean;
  className?: string;
  emphasis?: boolean;
  tone?: "auto" | "none";
}) {
  const colored = signed && tone !== "none" && paise !== undefined;
  return (
    <div className={cn("min-w-0 rounded-[10px] bg-surface-1 px-3.5 py-3 ring-1 ring-hairline", className)}>
      <div className="truncate text-[10.5px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">{label}</div>
      {loading || paise === undefined ? (
        <Skeleton className="mt-2 h-6 w-28" />
      ) : (
        <div
          className={cn(
            "mt-1 truncate tracking-tight",
            emphasis ? "text-[22px] font-semibold" : "text-lg font-semibold",
            colored && (paise > 0 ? "text-profit" : paise < 0 ? "text-loss" : "text-foreground"),
          )}
        >
          <Money paise={paise} signed={signed} />
        </div>
      )}
      {sub && <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}
