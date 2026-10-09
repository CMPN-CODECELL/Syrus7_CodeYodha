"use client";

import { useMemo, useState } from "react";
import { Crosshair, MessageSquarePlus } from "lucide-react";
import { useRevise } from "@/components/approval/proposal-actions";
import { Button } from "@/components/ui/button";
import { PageContainer, PageHeader } from "@/components/ui/panel";
import { EmptyState, ErrorState, SkeletonCard } from "@/components/ui/states";
import { useMandates } from "@/lib/queries";
import { useLiveStore } from "@/lib/store";
import type { MandateView } from "@/lib/types";
import { cn } from "@/lib/utils";
import { MandateCard } from "./mandate-card";

const FILTERS: { id: string; label: string; states: MandateView["state"][] }[] = [
  { id: "live", label: "Active", states: ["ACTIVE", "TRIGGERED", "EXECUTING", "PENDING_APPROVAL"] },
  { id: "review", label: "Needs review", states: ["NEEDS_REVIEW"] },
  { id: "history", label: "History", states: ["FIRED", "COMPLETED", "EXPIRED", "CANCELLED", "FAILED"] },
];

export function MandatesView() {
  const { data, error, isPending, refetch } = useMandates();
  const live = useLiveStore((s) => s.mandates);
  const [filter, setFilter] = useState("live");
  const revise = useRevise();

  const list = useMemo(() => (data ?? []).map((m) => live[m.mandate_id] ? { ...m, ...live[m.mandate_id], proposal_id: m.proposal_id } : m), [data, live]);
  const counts = Object.fromEntries(FILTERS.map((f) => [f.id, list.filter((m) => f.states.includes(m.state)).length]));
  const shown = list.filter((m) => FILTERS.find((f) => f.id === filter)?.states.includes(m.state));

  return (
    <PageContainer>
      <PageHeader
        icon={Crosshair}
        title="Mandates"
        description="Standing instructions you countersigned once. Each fires at most once, only on a fresh tick, and never after the system was offline: those move to review."
        actions={
          <Button variant="outline" size="sm" onClick={() => revise("If Reliance drops below 2800, buy 10")}>
            <MessageSquarePlus /> New mandate in Copilot
          </Button>
        }
      />
      <div className="flex gap-1.5" role="tablist" aria-label="Mandate filter">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            role="tab"
            aria-selected={filter === f.id}
            onClick={() => setFilter(f.id)}
            className={cn(
              "flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs ring-1 transition-colors",
              filter === f.id ? "bg-foreground/[0.07] text-foreground ring-hairline-strong" : "text-muted-foreground ring-hairline hover:text-foreground",
              f.id === "review" && counts.review > 0 && filter !== f.id && "text-warning ring-warning/30",
            )}
          >
            {f.label}
            <span className="num text-subtle">{counts[f.id]}</span>
          </button>
        ))}
      </div>
      {isPending ? (
        <div className="grid gap-4 md:grid-cols-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      ) : error ? (
        <ErrorState error={error} title="Mandates unavailable" onRetry={() => void refetch()} />
      ) : shown.length === 0 ? (
        <div className="panel">
          <EmptyState
            icon={Crosshair}
            title={filter === "live" ? "No active standing instructions" : filter === "review" ? "Nothing needs review" : "No mandate history yet"}
            description='Ask the copilot something like "If Reliance drops below 2800, buy 10" or "Tell me when any holding falls 3% in a day", then countersign to arm it.'
          />
        </div>
      ) : (
        <div className="grid items-start gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {shown.map((m) => (
            <MandateCard key={m.mandate_id} mandate={m} />
          ))}
        </div>
      )}
    </PageContainer>
  );
}
