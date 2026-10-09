"use client";

import { useState } from "react";
import { MessageSquarePlus, Workflow } from "lucide-react";
import { useRevise } from "@/components/approval/proposal-actions";
import { Button } from "@/components/ui/button";
import { PageContainer, PageHeader } from "@/components/ui/panel";
import { EmptyState, ErrorState, SkeletonCard } from "@/components/ui/states";
import { usePlans } from "@/lib/queries";
import type { ProposalStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { PlanCard } from "./plan-card";

const FILTERS: { id: string; label: string; statuses: ProposalStatus[] }[] = [
  { id: "pending", label: "Awaiting signature", statuses: ["AWAITING_APPROVAL", "BLOCKED"] },
  { id: "running", label: "Executing", statuses: ["APPROVED", "EXECUTING"] },
  { id: "done", label: "Finished", statuses: ["COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "REJECTED_BY_USER", "EXPIRED", "CANCELLED"] },
];

export function PlansView() {
  const { data, error, isPending, refetch } = usePlans();
  const [filter, setFilter] = useState("all");
  const revise = useRevise();
  const plans = data ?? [];
  const shown = filter === "all" ? plans : plans.filter((p) => FILTERS.find((f) => f.id === filter)?.statuses.includes(p.status));
  return (
    <PageContainer className="max-w-[1100px]">
      <PageHeader
        icon={Workflow}
        title="Plans"
        description="Multi-step plans run in order under one countersignature. A signed plan is immutable; it stops on failure and never places compensating trades on its own."
        actions={
          <Button variant="outline" size="sm" onClick={() => revise("Sell 20 INFY and buy 5 TCS")}>
            <MessageSquarePlus /> New plan in Copilot
          </Button>
        }
      />
      <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Plan filter">
        {[{ id: "all", label: "All", statuses: [] as ProposalStatus[] }, ...FILTERS].map((f) => {
          const n = f.id === "all" ? plans.length : plans.filter((p) => f.statuses.includes(p.status)).length;
          return (
            <button
              key={f.id}
              type="button"
              role="tab"
              aria-selected={filter === f.id}
              onClick={() => setFilter(f.id)}
              className={cn(
                "flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs ring-1 transition-colors",
                filter === f.id ? "bg-foreground/[0.07] text-foreground ring-hairline-strong" : "text-muted-foreground ring-hairline hover:text-foreground",
              )}
            >
              {f.label}
              <span className="num text-subtle">{n}</span>
            </button>
          );
        })}
      </div>
      {isPending ? (
        <SkeletonCard lines={6} />
      ) : error ? (
        <ErrorState error={error} title="Plans unavailable" onRetry={() => void refetch()} />
      ) : shown.length === 0 ? (
        <div className="panel">
          <EmptyState
            icon={Workflow}
            title="No plans here"
            description='Ask the copilot for several orders at once, for example "Sell 20 INFY and buy 5 TCS". Step 2 runs only after step 1 fills.'
          />
        </div>
      ) : (
        <div className="space-y-4">
          {shown.map((p) => (
            <PlanCard key={p.id} proposal={p} />
          ))}
        </div>
      )}
    </PageContainer>
  );
}
