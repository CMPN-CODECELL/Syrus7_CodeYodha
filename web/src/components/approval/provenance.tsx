"use client";

import { TriangleAlert } from "lucide-react";
import { TONE_CHIP } from "@/components/ui/chip";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { maskUccsIn } from "@/lib/broker";
import { SOURCE } from "@/lib/status";
import type { FieldRow, Proposal, Source } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ProvenanceChip({ source, compact }: { source: Source; compact?: boolean }) {
  const s = SOURCE[source] ?? SOURCE.system_default;
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            className={cn(
              "inline-flex h-5 shrink-0 items-center gap-1 rounded-md px-1.5 text-[10.5px] font-medium ring-1 ring-inset transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring",
              TONE_CHIP[s.tone],
            )}
            aria-label={`Source: ${s.label}. Where did this value come from?`}
          />
        }
      >
        <s.icon className="size-3" aria-hidden />
        {!compact && <span>{s.label}</span>}
      </TooltipTrigger>
      <TooltipContent side="left" className="max-w-64 flex-col items-start gap-1 py-2">
        <span className="text-[10px] font-semibold tracking-wider uppercase opacity-70">Where did this value come from?</span>
        <span className="font-medium">{s.label}</span>
        <span className="opacity-80">{s.explain}</span>
      </TooltipContent>
    </Tooltip>
  );
}

/** Field rows of the approval card, each with its provenance. Flagged rows are highlighted. */
export function FieldRows({
  fields,
  disagreements = [],
  className,
}: {
  fields: FieldRow[];
  disagreements?: Proposal["interpretation"]["disagreements"];
  className?: string;
}) {
  return (
    <dl className={cn("divide-y divide-hairline overflow-hidden rounded-lg ring-1 ring-hairline", className)}>
      {fields.map((f) => {
        const disagreement = disagreements.find((d) => d.field === f.key);
        const severe = !!disagreement || (f.flagged && f.source === "model_inferred");
        return (
          <div
            key={f.key}
            className={cn(
              "group/row relative flex flex-col gap-1 px-3 py-2",
              f.flagged && (severe ? "bg-danger/[0.08]" : "bg-warning/[0.08]"),
            )}
          >
            {f.flagged && (
              <span className={cn("absolute inset-y-0 left-0 w-[3px]", severe ? "bg-danger" : "bg-warning")} aria-hidden />
            )}
            <div className="flex min-h-6 items-center gap-3">
              <dt className="w-28 shrink-0 text-xs text-muted-foreground">{f.label}</dt>
              <dd
                className={cn(
                  "num min-w-0 flex-1 truncate text-[13px] font-semibold",
                  f.flagged && (severe ? "text-danger" : "text-warning"),
                )}
                title={maskUccsIn(f.display)}
              >
                {maskUccsIn(f.display)}
              </dd>
              <ProvenanceChip source={f.source} />
            </div>
            {(f.note || disagreement) && (
              <p className={cn("flex items-start gap-1.5 pl-[124px] text-xs leading-snug", severe ? "text-danger" : f.flagged ? "text-warning" : "text-muted-foreground")}>
                {f.flagged && <TriangleAlert className="mt-0.5 size-3 shrink-0" aria-hidden />}
                <span>
                  {f.note ??
                    (disagreement ? `You said ${disagreement.trader_value}; the AI proposed ${disagreement.model_value}.` : null)}
                </span>
              </p>
            )}
          </div>
        );
      })}
    </dl>
  );
}
