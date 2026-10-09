import { Info, Lock, Scale, TriangleAlert } from "lucide-react";
import type { Issue, Proposal } from "@/lib/types";
import { cn } from "@/lib/utils";

const ORDER = { BLOCK: 0, WARN: 1, INFO: 2 } as const;

export function IssuesList({
  issues,
  disagreements = [],
  className,
}: {
  issues: Issue[];
  disagreements?: Proposal["interpretation"]["disagreements"];
  className?: string;
}) {
  const sorted = [...issues].sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);
  if (!sorted.length && !disagreements.length) return null;
  return (
    <ul className={cn("space-y-1.5", className)} aria-label="Verifier findings">
      {disagreements.map((d) => (
        <li
          key={`dis-${d.field}`}
          className="flex items-start gap-2 rounded-md bg-danger/[0.09] px-2.5 py-2 text-xs ring-1 ring-danger/30"
        >
          <Scale className="mt-0.5 size-3.5 shrink-0 text-danger" aria-hidden />
          <span className="min-w-0">
            <span className="font-semibold text-danger">Interpretation disagreement on {d.field}: </span>
            <span className="text-foreground/90">
              you said <span className="num font-semibold">{d.trader_value}</span>, the AI proposed{" "}
              <span className="num font-semibold">{d.model_value}</span>. Disagreements always block.
            </span>
          </span>
        </li>
      ))}
      {sorted.map((i, idx) => {
        const Icon = i.severity === "BLOCK" ? Lock : i.severity === "WARN" ? TriangleAlert : Info;
        return (
          <li
            key={`${i.code}-${idx}`}
            className={cn(
              "flex items-start gap-2 rounded-md px-2.5 py-2 text-xs ring-1",
              i.severity === "BLOCK" && "bg-danger/[0.09] ring-danger/30",
              i.severity === "WARN" && "bg-warning/[0.08] ring-warning/30",
              i.severity === "INFO" && "bg-info/[0.06] ring-info/20",
            )}
          >
            <Icon
              className={cn(
                "mt-0.5 size-3.5 shrink-0",
                i.severity === "BLOCK" ? "text-danger" : i.severity === "WARN" ? "text-warning" : "text-info",
              )}
              aria-hidden
            />
            <span className="min-w-0 flex-1 leading-snug">
              <span
                className={cn(
                  "mr-1.5 font-mono text-[10px] font-semibold tracking-wide",
                  i.severity === "BLOCK" ? "text-danger" : i.severity === "WARN" ? "text-warning" : "text-info",
                )}
              >
                {i.severity}
              </span>
              <span className="text-foreground/90">{i.message}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}
