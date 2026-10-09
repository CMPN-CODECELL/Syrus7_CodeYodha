"use client";

import { useMemo, useState } from "react";
import { Activity, Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { PageContainer, PageHeader, Panel } from "@/components/ui/panel";
import { ErrorState, SkeletonRows } from "@/components/ui/states";
import { ConnectionIndicator } from "@/components/shell/status-indicators";
import { OUTCOME_GROUPS } from "@/lib/status";
import { useOutcomes } from "@/lib/queries";
import { useLiveStore } from "@/lib/store";
import type { Severity } from "@/lib/types";
import { cn } from "@/lib/utils";
import { OutcomeFeed } from "./outcome-feed";

const SEVERITIES: { id: Severity | "all"; label: string; dot: string }[] = [
  { id: "all", label: "All", dot: "bg-foreground/50" },
  { id: "success", label: "Success", dot: "bg-profit" },
  { id: "info", label: "Info", dot: "bg-info" },
  { id: "warning", label: "Warning", dot: "bg-warning" },
  { id: "danger", label: "Danger", dot: "bg-danger" },
];

export function OutcomesView() {
  const seeded = useOutcomes();
  const items = useLiveStore((s) => s.outcomes);
  const [sev, setSev] = useState<Severity | "all">("all");
  const [group, setGroup] = useState<string>("all");
  const [q, setQ] = useState("");

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items.length };
    for (const o of items) c[o.severity] = (c[o.severity] ?? 0) + 1;
    return c;
  }, [items]);

  const filtered = useMemo(() => {
    const kinds = OUTCOME_GROUPS.find((g) => g.id === group)?.kinds;
    const needle = q.trim().toLowerCase();
    return items.filter(
      (o) =>
        (sev === "all" || o.severity === sev) &&
        (!kinds || kinds.includes(o.kind)) &&
        (!needle || `${o.title} ${o.detail} ${o.broker_order_id ?? ""} ${o.proposal_id ?? ""}`.toLowerCase().includes(needle)),
    );
  }, [items, sev, group, q]);

  return (
    <PageContainer className="max-w-[1100px]">
      <PageHeader
        icon={Activity}
        title="Outcomes"
        description="Everything that happened after a countersignature, live and newest first: acknowledgements, fills, rejections, reconciliations, mandate triggers and blocks."
        actions={<ConnectionIndicator />}
      />
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by severity">
          {SEVERITIES.map((s) => (
            <button
              key={s.id}
              type="button"
              aria-pressed={sev === s.id}
              onClick={() => setSev(s.id)}
              className={cn(
                "flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs ring-1 transition-colors",
                sev === s.id ? "bg-foreground/[0.07] text-foreground ring-hairline-strong" : "text-muted-foreground ring-hairline hover:text-foreground",
              )}
            >
              <span className={cn("size-1.5 rounded-full", s.dot)} />
              {s.label}
              <span className="num text-subtle">{counts[s.id] ?? 0}</span>
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by type">
          {[{ id: "all", label: "Everything" }, ...OUTCOME_GROUPS].map((g) => (
            <button
              key={g.id}
              type="button"
              aria-pressed={group === g.id}
              onClick={() => setGroup(g.id)}
              className={cn(
                "h-8 rounded-lg px-2.5 text-xs ring-1 transition-colors",
                group === g.id ? "bg-human/10 text-human ring-human/35" : "text-muted-foreground ring-hairline hover:text-foreground",
              )}
            >
              {g.label}
            </button>
          ))}
        </div>
        <div className="relative lg:ml-auto lg:w-64">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-subtle" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search tag, order id, symbol" className="h-8 pl-8 text-xs" aria-label="Search outcomes" />
        </div>
      </div>
      <Panel>
        {seeded.isPending && items.length === 0 ? (
          <SkeletonRows rows={8} />
        ) : seeded.error && items.length === 0 ? (
          <ErrorState error={seeded.error} title="Outcome history unavailable" onRetry={() => void seeded.refetch()} className="m-3" />
        ) : (
          <OutcomeFeed
            items={filtered}
            emptyHint={items.length ? "No outcomes match these filters." : undefined}
          />
        )}
      </Panel>
    </PageContainer>
  );
}
