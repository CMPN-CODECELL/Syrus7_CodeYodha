"use client";

import { Calculator, Scale, TrendingDown, Workflow, type LucideIcon } from "lucide-react";
import { Eyebrow } from "@/components/ui/panel";
import { inr, pct, qty } from "@/lib/format";
import type { Issue, PlanDetail, PlanFact, PlanMathColumn, Proposal, ProposalStep } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Notes the verifier attaches to a plan it computed itself: the method, the scope and the resulting weights. */
export const PLAN_NOTE_RE = /^(EXIT_|REBALANCE_)/;

/**
 * Which computed plan this is: from an explicit plan object ("exit_losing_intraday", "exit-losing-intraday", ...) or
 * from the verifier's notes (EXIT_SCOPE, REBALANCE_METHOD, REBALANCE_RESULT). Null for a plan the trader dictated.
 */
export function planKind(p: Proposal): "exit_losing_intraday" | "rebalance" | "custom" | null {
  const k = p.plan?.kind?.toLowerCase().replace(/-/g, "_");
  if (k) {
    if (k.startsWith("exit")) return "exit_losing_intraday";
    if (k.startsWith("rebalance")) return "rebalance";
    return "custom";
  }
  if (p.issues.some((i) => i.code.startsWith("EXIT_"))) return "exit_losing_intraday";
  if (p.issues.some((i) => i.code.startsWith("REBALANCE_"))) return "rebalance";
  return null;
}

/** "Rebalance to at most 20% per stock: (1) SELL ..." carries its title before the first step. */
export function planTitle(p: Proposal): string | null {
  if (p.plan?.title) return p.plan.title;
  if (!planKind(p)) return null;
  const m = p.summary.match(/^(.+?):\s*\(1\)/);
  return m ? m[1].trim() : null;
}

interface WeightRow {
  symbol: string;
  now: number;
  after: number;
}

/** Parses "Weights now → after this plan, at live prices: RELIANCE 27.60% → 20.00%, INFY 12.10% → 14.30%". */
function parseWeights(message: string): WeightRow[] | null {
  const body = message.split(/:\s*/).slice(1).join(": ");
  if (!body) return null;
  const rows: WeightRow[] = [];
  for (const part of body.replace(/\.$/, "").split(/,\s*/)) {
    const m = part.trim().match(/^(\S+)\s+(-?[\d.]+)%\s*(?:→|->)\s*(-?[\d.]+)%$/);
    if (!m) return null;
    rows.push({ symbol: m[1], now: Number(m[2]), after: Number(m[3]) });
  }
  return rows.length ? rows : null;
}

function WeightsTable({ rows, capPct }: { rows: WeightRow[]; capPct: number | null }) {
  return (
    <div className="scrollbar-thin overflow-x-auto rounded-md ring-1 ring-hairline">
      <table className="w-full min-w-max border-collapse text-[12px]">
        <thead className="bg-surface-1">
          <tr>
            <th scope="col" className="border-b border-hairline px-2.5 py-1.5 text-left text-[10.5px] font-semibold tracking-wide text-muted-foreground">
              Stock
            </th>
            <th scope="col" className="border-b border-hairline px-2.5 py-1.5 text-right text-[10.5px] font-semibold tracking-wide text-muted-foreground">
              Weight now
            </th>
            <th scope="col" className="border-b border-hairline px-2.5 py-1.5 text-right text-[10.5px] font-semibold tracking-wide text-muted-foreground">
              After this plan
            </th>
            <th scope="col" className="w-[40%] border-b border-hairline px-2.5 py-1.5 text-left text-[10.5px] font-semibold tracking-wide text-muted-foreground">
              {capPct !== null ? `Cap ${capPct}%` : ""}
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-hairline">
          {rows.map((r) => {
            const over = capPct !== null && r.now > capPct;
            const scale = Math.max(capPct ?? 0, ...rows.map((x) => x.now), 1) * 1.1;
            return (
              <tr key={r.symbol}>
                <td className="px-2.5 py-1.5 font-medium">{r.symbol}</td>
                <td className={cn("num px-2.5 py-1.5 text-right", over && "text-warning")}>{r.now.toFixed(2)}%</td>
                <td className="num px-2.5 py-1.5 text-right">{r.after.toFixed(2)}%</td>
                <td className="px-2.5 py-1.5">
                  <div className="relative h-2 rounded-full bg-foreground/[0.06]" aria-hidden>
                    <div className={cn("absolute inset-y-0 left-0 rounded-full", over ? "bg-warning/40" : "bg-foreground/15")} style={{ width: `${(r.now / scale) * 100}%` }} />
                    <div className="absolute inset-y-0 left-0 rounded-full bg-info" style={{ width: `${(r.after / scale) * 100}%` }} />
                    {capPct !== null && <div className="absolute -inset-y-0.5 w-px bg-human" style={{ left: `${(capPct / scale) * 100}%` }} />}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** The verifier's notes on a computed plan, with the weights note drawn as a table. */
export function PlanNotes({ proposal, className }: { proposal: Proposal; className?: string }) {
  const notes = proposal.issues.filter((i) => PLAN_NOTE_RE.test(i.code));
  if (!notes.length) return null;
  const capM = (planTitle(proposal) ?? "").match(/([\d.]+)%/);
  const capPct = capM ? Number(capM[1]) : null;
  return (
    <div className={cn("space-y-2.5 rounded-lg bg-surface-2/50 p-3 ring-1 ring-hairline", className)}>
      <Eyebrow className="flex items-center gap-1.5">
        <Calculator className="size-3.5 text-info" aria-hidden /> How this plan was computed
      </Eyebrow>
      {notes.map((n: Issue) => {
        const weights = n.code === "REBALANCE_RESULT" ? parseWeights(n.message) : null;
        return weights ? (
          <WeightsTable key={n.code} rows={weights} capPct={capPct} />
        ) : (
          <p key={n.code + n.message.slice(0, 16)} className={cn("text-[12.5px] leading-snug", n.severity === "INFO" ? "text-foreground/90" : "text-warning")}>
            {n.message}
          </p>
        );
      })}
    </div>
  );
}

export const PLAN_KIND_LOOK: Record<"exit_losing_intraday" | "rebalance" | "custom", { title: string; icon: LucideIcon }> = {
  exit_losing_intraday: { title: "Exit losing intraday positions", icon: TrendingDown },
  rebalance: { title: "Rebalance", icon: Scale },
  custom: { title: "Plan", icon: Workflow },
};

function factValue(f: PlanFact): string {
  if (f.paise !== null && f.paise !== undefined) return inr(f.paise);
  if (f.bps !== null && f.bps !== undefined) return pct(f.bps);
  if (f.qty !== null && f.qty !== undefined) return qty(f.qty);
  return f.text ?? "-";
}

function cellValue(v: string | number | null | undefined, format: PlanMathColumn["format"]): string {
  if (v === null || v === undefined || v === "") return "-";
  if (typeof v === "string") return v;
  if (format === "paise") return inr(v);
  if (format === "bps") return pct(v);
  if (format === "qty") return qty(v);
  return String(v);
}

function cellTone(v: string | number | null | undefined, col: PlanMathColumn): string | undefined {
  if (typeof v !== "number") return undefined;
  if ((col.format === "paise" || col.format === "bps") && /p&l|pnl|change|loss|gain/i.test(col.label)) {
    return v < 0 ? "text-loss" : v > 0 ? "text-profit" : undefined;
  }
  return undefined;
}

/** The arithmetic behind a computed plan, exactly as the backend sent it: facts, a table, and notes. */
export function PlanMath({ plan, className }: { plan: PlanDetail; className?: string }) {
  const cols = plan.columns ?? [];
  const rows = plan.rows ?? [];
  const facts = plan.facts ?? [];
  if (!plan.summary && !facts.length && !rows.length && !plan.notes?.length) return null;
  return (
    <div className={cn("space-y-2.5 rounded-lg bg-surface-2/50 p-3 ring-1 ring-hairline", className)}>
      <Eyebrow className="flex items-center gap-1.5">
        <Calculator className="size-3.5 text-info" aria-hidden /> How this plan was computed
      </Eyebrow>
      {plan.summary && <p className="text-[12.5px] leading-snug text-foreground/90">{plan.summary}</p>}
      {facts.length > 0 && (
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {facts.map((f) => (
            <div key={f.label} className="rounded-md bg-background/40 px-2 py-1.5 ring-1 ring-hairline">
              <dt className="text-[10.5px] text-muted-foreground">{f.label}</dt>
              <dd className="num truncate text-[13px] font-medium">{factValue(f)}</dd>
            </div>
          ))}
        </dl>
      )}
      {cols.length > 0 && rows.length > 0 && (
        <div className="scrollbar-thin max-h-[320px] overflow-auto rounded-md ring-1 ring-hairline">
          <table className="w-full min-w-max border-collapse text-[12px]">
            <thead className="sticky top-0 bg-surface-1">
              <tr>
                {cols.map((c) => (
                  <th
                    key={c.key}
                    scope="col"
                    className={cn(
                      "border-b border-hairline px-2.5 py-1.5 text-[10.5px] font-semibold tracking-wide whitespace-nowrap text-muted-foreground",
                      c.format && c.format !== "text" ? "text-right" : "text-left",
                    )}
                  >
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {rows.map((r, i) => (
                <tr key={i} className="hover:bg-foreground/[0.03]">
                  {cols.map((c) => (
                    <td
                      key={c.key}
                      className={cn(
                        "px-2.5 py-1.5 whitespace-nowrap",
                        c.format && c.format !== "text" ? "num text-right" : "text-left",
                        cellTone(r[c.key], c),
                      )}
                    >
                      {cellValue(r[c.key], c.format)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {plan.notes && plan.notes.length > 0 && (
        <ul className="space-y-1 text-[11.5px] leading-snug text-muted-foreground">
          {plan.notes.map((n) => (
            <li key={n} className="flex gap-1.5">
              <span className="mt-[7px] size-1 shrink-0 rounded-full bg-muted-foreground/60" aria-hidden />
              {n}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export interface PlanTally {
  filled: number;
  partial: number;
  rejected: number;
  notPlaced: number;
  review: number;
  skipped: number;
  open: number;
  pending: number;
}

export function tallySteps(steps: ProposalStep[]): PlanTally {
  const t: PlanTally = { filled: 0, partial: 0, rejected: 0, notPlaced: 0, review: 0, skipped: 0, open: 0, pending: 0 };
  for (const s of steps) {
    switch (s.state) {
      case "FILLED":
        t.filled++;
        break;
      case "PARTIALLY_FILLED":
        t.partial++;
        break;
      case "REJECTED":
      case "FAILED":
      case "CANCELLED":
      case "BLOCKED":
        t.rejected++;
        break;
      case "NOT_PLACED":
        t.notPlaced++;
        break;
      case "AMBIGUOUS":
      case "NEEDS_REVIEW":
        t.review++;
        break;
      case "SKIPPED":
        t.skipped++;
        break;
      case "OPEN":
      case "SUBMITTED":
        t.open++;
        break;
      default:
        t.pending++;
    }
  }
  return t;
}
