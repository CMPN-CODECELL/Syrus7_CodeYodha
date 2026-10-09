"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { qk, useOutcomes, useWatchlist } from "@/lib/queries";
import { useLiveStore } from "@/lib/store";
import type { LiveEvent, OutcomeEvent } from "@/lib/types";

const ACCOUNT_KINDS = new Set<OutcomeEvent["kind"]>([
  "ORDER_ACCEPTED",
  "ORDER_PARTIALLY_FILLED",
  "ORDER_FILLED",
  "ORDER_REJECTED",
  "ORDER_CANCELLED",
  "RECONCILED",
  "PLAN_COMPLETED",
  "PLAN_PARTIALLY_COMPLETED",
  "PLAN_FAILED",
]);

const TOAST_KINDS = new Set<OutcomeEvent["kind"]>([
  "ORDER_FILLED",
  "ORDER_PARTIALLY_FILLED",
  "ORDER_REJECTED",
  "RECONCILED",
  "MANDATE_TRIGGERED",
  "MANDATE_NEEDS_REVIEW",
  "ALERT",
  "PLAN_COMPLETED",
  "PLAN_PARTIALLY_COMPLETED",
  "PLAN_FAILED",
  "KILL_SWITCH",
]);

/**
 * Owns the single shared SSE connection to /api/events. Feeds the zustand live store and
 * invalidates TanStack Query caches for whatever changed.
 */
export function LiveBridge() {
  const qc = useQueryClient();
  const ingest = useLiveStore((s) => s.ingest);
  const setConn = useLiveStore((s) => s.setConn);
  const seedTicks = useLiveStore((s) => s.seedTicks);
  const seedOutcomes = useLiveStore((s) => s.seedOutcomes);
  const pending = useRef<{ account: boolean; audit: boolean; timer: ReturnType<typeof setTimeout> | null }>({
    account: false,
    audit: false,
    timer: null,
  });

  const watchlist = useWatchlist();
  const outcomes = useOutcomes();

  useEffect(() => {
    if (watchlist.data) seedTicks(watchlist.data.map((q) => q.tick));
  }, [watchlist.data, seedTicks]);

  useEffect(() => {
    if (outcomes.data) seedOutcomes(outcomes.data);
  }, [outcomes.data, seedOutcomes]);

  useEffect(() => {
    const p = pending.current;
    const flush = () => {
      p.timer = null;
      if (p.account) void qc.invalidateQueries({ queryKey: qk.account });
      if (p.audit) void qc.invalidateQueries({ queryKey: qk.audit });
      p.account = false;
      p.audit = false;
    };
    const schedule = (what: "account" | "audit") => {
      p[what] = true;
      if (!p.timer) p.timer = setTimeout(flush, 600);
    };

    const onEvent = (e: LiveEvent) => {
      ingest(e);
      switch (e.type) {
        case "outcome": {
          const o = e.data;
          if (ACCOUNT_KINDS.has(o.kind)) schedule("account");
          if (o.proposal_id) void qc.invalidateQueries({ queryKey: qk.proposal(o.proposal_id) });
          if (o.kind.startsWith("MANDATE") || o.kind === "ALERT") void qc.invalidateQueries({ queryKey: qk.mandates });
          if (o.kind.startsWith("PLAN")) void qc.invalidateQueries({ queryKey: qk.plans });
          if (o.kind === "KILL_SWITCH") void qc.invalidateQueries({ queryKey: qk.security });
          if (TOAST_KINDS.has(o.kind)) {
            const fn =
              o.severity === "danger" ? toast.error : o.severity === "warning" ? toast.warning : o.severity === "success" ? toast.success : toast.info;
            fn(o.title, { description: o.detail });
          }
          break;
        }
        case "proposal":
          void qc.invalidateQueries({ queryKey: qk.proposal(e.data.id) });
          void qc.invalidateQueries({ queryKey: qk.proposalsAll });
          void qc.invalidateQueries({ queryKey: qk.plans });
          break;
        case "mandate":
          void qc.invalidateQueries({ queryKey: qk.mandates });
          break;
        case "security":
          void qc.invalidateQueries({ queryKey: qk.securityEvents });
          if (e.data.severity === "critical") toast.error(e.data.kind.replace(/_/g, " "), { description: e.data.message });
          break;
        case "monitor":
          // Kept in the live store only. Writing it into the security-status query would
          // restart that query's refetch interval on every heartbeat and stop it polling.
          break;
        case "audit":
          schedule("audit");
          break;
        default:
          break;
      }
    };

    const dispose = api.subscribe({
      onEvent,
      onStatus: (s, d) => setConn(s, d?.attempt ?? 0, d?.retryInMs),
    });
    return () => {
      dispose();
      if (p.timer) clearTimeout(p.timer);
    };
  }, [qc, ingest, setConn]);

  return null;
}
