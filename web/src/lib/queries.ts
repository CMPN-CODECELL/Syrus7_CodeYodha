// TanStack Query hooks over the typed API. Query keys are centralised so live events can
// invalidate exactly what changed.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import { api, type OrdersFilter } from "./api";
import { IS_MOCK } from "./env";
import { isApiError } from "./errors";
import { useChatStore } from "./store";
import type {
  ApprovalMethod,
  BrokerAccount,
  BrokerFault,
  BrokerKind,
  BrokerStatus,
  FeedScenario,
  Proposal,
  ProposalKind,
  ProposalStatus,
  SecurityEventScope,
} from "./types";

export const qk = {
  me: ["me"] as const,
  summary: ["account", "summary"] as const,
  holdings: ["account", "holdings"] as const,
  positions: ["account", "positions"] as const,
  orders: (f: OrdersFilter) => ["account", "orders", f] as const,
  account: ["account"] as const,
  watchlist: ["market", "watchlist"] as const,
  history: (s: string) => ["market", "history", s] as const,
  optionChain: (e: string | undefined) => ["market", "option-chain", e ?? "nearest"] as const,
  quote: (s: string) => ["market", "quote", s] as const,
  proposal: (id: string) => ["proposal", id] as const,
  proposals: (p: object) => ["proposals", p] as const,
  proposalsAll: ["proposals"] as const,
  mandates: ["mandates"] as const,
  plans: ["plans"] as const,
  outcomes: ["outcomes"] as const,
  audit: ["audit"] as const,
  security: ["security", "status"] as const,
  securityEvents: ["security", "events"] as const,
  sessions: ["sessions"] as const,
  scenarios: ["attack-lab", "scenarios"] as const,
  runs: ["attack-lab", "runs"] as const,
  scoreboard: ["attack-lab", "scoreboard"] as const,
  demo: ["demo", "state"] as const,
  brokerAccounts: ["broker", "accounts"] as const,
  brokerStatus: ["broker", "status"] as const,
};

export const ACTIVE_STATUSES: ProposalStatus[] = ["APPROVED", "EXECUTING"];

export function useMe() {
  return useQuery({
    queryKey: qk.me,
    queryFn: api.me,
    staleTime: 30_000,
    retry: (count, err) => !(isApiError(err) && (err.status === 401 || err.status === 403)) && count < 1,
  });
}

// The API sets the readable cs_csrf cookie together with the HttpOnly session cookie, so its presence is a hint
// that a session may exist. Public pages use it to skip /api/me (and its 401) for signed-out visitors.
const CSRF_COOKIE = /(?:^|;\s*)cs_csrf=/;
const noSubscribe = () => () => {};

/** null during server rendering and hydration (unknown), then whether a session cookie is present. */
export function useSessionHint(): boolean | null {
  return useSyncExternalStore(
    noSubscribe,
    () => IS_MOCK || CSRF_COOKIE.test(document.cookie),
    () => null,
  );
}

/**
 * /api/me for public pages (landing, sign-in, sign-up). Not requested at all without a session cookie; while it is
 * unknown or loading, `pending` is true so callers can render a neutral placeholder instead of signed-out buttons.
 */
export function useOptionalMe() {
  const hint = useSessionHint();
  const q = useQuery({
    queryKey: qk.me,
    queryFn: async () => {
      try {
        return await api.me();
      } catch (e) {
        // A leftover cookie from a session that has since ended: drop it so the next visit does not ask again.
        if (isApiError(e) && e.status === 401 && !IS_MOCK) document.cookie = "cs_csrf=; Max-Age=0; path=/; SameSite=Strict";
        throw e;
      }
    },
    enabled: hint === true,
    retry: false,
    staleTime: 30_000,
  });
  const me = q.isSuccess && !q.isError ? q.data : undefined;
  return { me, pending: hint === null || (hint && q.isPending), hint };
}

export function useAccountSummary() {
  return useQuery({ queryKey: qk.summary, queryFn: api.accountSummary, refetchInterval: 15_000 });
}
export function useHoldings() {
  return useQuery({ queryKey: qk.holdings, queryFn: api.holdings });
}
export function usePositions() {
  return useQuery({ queryKey: qk.positions, queryFn: api.positions });
}
export function useOrders(filter: OrdersFilter) {
  return useQuery({ queryKey: qk.orders(filter), queryFn: () => api.orders(filter) });
}
export function useWatchlist() {
  return useQuery({ queryKey: qk.watchlist, queryFn: api.watchlist, staleTime: 60_000 });
}
export function useHistory(symbol: string) {
  return useQuery({ queryKey: qk.history(symbol), queryFn: () => api.history(symbol, 300), staleTime: 5 * 60_000 });
}
export function useOptionChain(expiry: string | undefined) {
  return useQuery({
    queryKey: qk.optionChain(expiry),
    queryFn: () => api.optionChain({ expiry, width: 8 }),
    refetchInterval: 10_000,
  });
}

/** Proposal detail; polls every 1 s while the executor is working on it. */
export function useProposal(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.proposal(id ?? ""),
    queryFn: () => api.proposal(id as string),
    enabled: !!id,
    refetchInterval: (q) => {
      const p = q.state.data as Proposal | undefined;
      return p && ACTIVE_STATUSES.includes(p.status) ? 1000 : false;
    },
  });
}

export function useProposals(params: { kind?: ProposalKind; status?: ProposalStatus; limit?: number } = {}) {
  return useQuery({ queryKey: qk.proposals(params), queryFn: () => api.proposals(params) });
}
export function useMandates() {
  return useQuery({ queryKey: qk.mandates, queryFn: api.mandates });
}
export function usePlans() {
  return useQuery({
    queryKey: qk.plans,
    queryFn: api.plans,
    refetchInterval: (q) => ((q.state.data as Proposal[] | undefined)?.some((p) => ACTIVE_STATUSES.includes(p.status)) ? 1000 : false),
  });
}
export function useOutcomes() {
  return useQuery({ queryKey: qk.outcomes, queryFn: () => api.outcomes(100) });
}
export function useAudit() {
  return useQuery({ queryKey: qk.audit, queryFn: () => api.audit({ limit: 100 }) });
}
export function useSecurityStatus() {
  return useQuery({ queryKey: qk.security, queryFn: api.securityStatus, refetchInterval: 5000 });
}
/** Own account plus system-wide events; "all" also lists the shared Attack Lab sandbox's drills. */
export function useSecurityEvents(scope: SecurityEventScope = "mine") {
  return useQuery({ queryKey: [...qk.securityEvents, scope], queryFn: () => api.securityEvents(scope) });
}
export function useSessions() {
  return useQuery({ queryKey: qk.sessions, queryFn: api.sessions });
}
export function useScenarios() {
  return useQuery({ queryKey: qk.scenarios, queryFn: api.scenarios, staleTime: Infinity });
}
export function useAttackRuns() {
  return useQuery({ queryKey: qk.runs, queryFn: () => api.attackRuns(10) });
}
export function useScoreboard() {
  return useQuery({ queryKey: qk.scoreboard, queryFn: api.scoreboard });
}
/** Demo controls only exist when the backend runs with CS_DEMO_MODE=true. */
export function useDemoState(enabled = true) {
  return useQuery({ queryKey: qk.demo, queryFn: api.demoState, retry: false, staleTime: 10_000, enabled });
}

/** Whether presenter controls are available: /api/me says so, or (older API) /api/demo/state answers. */
export function useDemoMode(): boolean {
  const me = useMe();
  const flag = me.data?.demo_mode;
  const probe = useDemoState(me.isSuccess && flag === undefined);
  return flag ?? probe.isSuccess;
}

/**
 * GET /api/broker/accounts. Resolves to null when this server has no such endpoint yet (404), so callers can fall
 * back to the paper account from /api/me instead of showing an error.
 */
export function useBrokerAccounts(enabled = true) {
  return useQuery({
    queryKey: qk.brokerAccounts,
    enabled,
    queryFn: async (): Promise<BrokerAccount[] | null> => {
      try {
        return await api.brokerAccounts();
      } catch (e) {
        if (isApiError(e) && (e.status === 404 || e.status === 405)) return null;
        throw e;
      }
    },
    retry: (count, err) => !(isApiError(err) && err.status < 500) && count < 1,
    staleTime: 10_000,
    refetchInterval: (q) => (q.state.data ? 15_000 : false),
  });
}

export interface ActiveBroker {
  /** The active broker account, or null when the server does not report accounts. */
  account: BrokerAccount | null;
  accounts: BrokerAccount[];
  kind: BrokerKind;
  /** Prices stream live from the 021 sandbox for the active account. */
  live021: boolean;
  /** Real instruments are priced by the live 021 feed (true for paper accounts too while the gateway streams). */
  liveFeed: boolean;
  /** False when the server has no broker accounts endpoint (an older backend). */
  reported: boolean;
  isPending: boolean;
  error: unknown;
}

/** The trader's active broker, with fallbacks for an older backend: /api/me, then the security status. */
export function useActiveBroker(): ActiveBroker {
  const q = useBrokerAccounts();
  const me = useMe();
  const sec = useSecurityStatus();
  const accounts = q.data ?? [];
  const account = accounts.find((a) => a.active) ?? (accounts.length === 1 ? accounts[0] : null);
  const kind: BrokerKind = account?.kind ?? me.data?.account?.kind ?? sec.data?.broker.kind ?? "paper";
  return {
    account,
    accounts,
    kind,
    live021: account?.kind === "021" && account.status.market_data === "live",
    liveFeed: account?.status.market_data === "live",
    reported: Array.isArray(q.data),
    isPending: q.isPending,
    error: q.error,
  };
}

/**
 * GET /api/broker/status. Resolves to null when this server has no such endpoint (404), which the UI treats as
 * "the 021 gateway is not running here".
 */
export function useBrokerStatus(enabled = true) {
  return useQuery({
    queryKey: qk.brokerStatus,
    queryFn: async (): Promise<BrokerStatus | null> => {
      try {
        return await api.brokerStatus();
      } catch (e) {
        if (isApiError(e) && (e.status === 404 || e.status === 405)) return null;
        throw e;
      }
    },
    retry: (count, err) => !(isApiError(err) && err.status < 500) && count < 1,
    staleTime: 30_000,
    enabled,
  });
}

/** Broker kind for an account id (a proposal's canonical account), or null when unknown. */
export function useBrokerKindFor(accountId: string | null | undefined): BrokerKind | null {
  const q = useBrokerAccounts();
  const me = useMe();
  if (!accountId) return null;
  const hit = q.data?.find((a) => a.account_id === accountId);
  if (hit) return hit.kind;
  const mine = me.data?.account;
  if (mine && mine.id === accountId && mine.kind) return mine.kind;
  return null;
}

// Mutations

export function useSetActiveBroker() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (account_id: string) => api.setActiveBroker(account_id),
    onSuccess: () => {
      // Portfolio, orders, market data source and status all depend on the active account.
      void qc.invalidateQueries();
    },
  });
}

/** Disconnects the trader's own 021 account: the API deletes the stored password and switches back to paper. */
export function useUnlink021() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (account_id: string) => api.unlink021(account_id),
    onSuccess: (res) => {
      if (Array.isArray(res?.accounts)) qc.setQueryData(qk.brokerAccounts, res.accounts);
      // Like switching accounts: portfolio, orders, prices and /api/me all follow the active account.
      void qc.invalidateQueries();
    },
  });
}

export function useRejectProposal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.rejectProposal(id),
    onSuccess: (p) => {
      qc.setQueryData(qk.proposal(p.id), p);
      void qc.invalidateQueries({ queryKey: qk.proposalsAll });
      void qc.invalidateQueries({ queryKey: qk.plans });
    },
  });
}

export function useApprovalStart() {
  return useMutation({
    mutationFn: ({ id, method }: { id: string; method: ApprovalMethod }) => api.approvalStart(id, method),
  });
}

export function useCancelMandate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.cancelMandate(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.mandates });
      void qc.invalidateQueries({ queryKey: qk.proposalsAll });
    },
  });
}

export function useKillSwitch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ engaged, pin }: { engaged: boolean; pin?: string }) => api.killSwitch(engaged, pin),
    onSuccess: (status) => {
      qc.setQueryData(qk.security, status);
      void qc.invalidateQueries({ queryKey: qk.securityEvents });
    },
  });
}

export function useDemoActions() {
  const qc = useQueryClient();
  const refreshAll = () => void qc.invalidateQueries();
  return {
    reset: useMutation({
      mutationFn: api.demoReset,
      onSuccess: () => {
        // The reset deleted this trader's conversation too: start the chat empty instead of showing dead cards.
        useChatStore.getState().reset();
        refreshAll();
      },
    }),
    feed: useMutation({
      mutationFn: ({ scenario, symbol }: { scenario: FeedScenario; symbol?: string }) => api.demoFeed(scenario, symbol),
      onSuccess: () => void qc.invalidateQueries({ queryKey: qk.demo }),
    }),
    fault: useMutation({
      mutationFn: ({ mode, count }: { mode: BrokerFault; count?: number }) => api.demoBrokerFault(mode, count),
      onSuccess: () => void qc.invalidateQueries({ queryKey: qk.demo }),
    }),
  };
}
