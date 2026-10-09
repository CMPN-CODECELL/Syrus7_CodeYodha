import { create } from "zustand";
import { api } from "./api";
import { errorMessage, isApiError } from "./errors";
import type { ConnStatus } from "./sse";
import type {
  Card,
  ChatEngine,
  ChatEvent,
  LiveEvent,
  MandateView,
  MonitorStatus,
  OutcomeEvent,
  ProposalStatus,
  SecurityEvent,
  Tick,
} from "./types";

// ---------------------------------------------------------------------------
// Live store: fed by the single shared SSE connection to /api/events.

export interface TickMeta {
  /** Direction of the last change: 1 up, -1 down, 0 unchanged. */
  dir: 1 | -1 | 0;
  /** Per-instrument counter; its parity picks the flash keyframe so each tick restarts it. */
  n: number;
}

const MAX_OUTCOMES = 300;
const MAX_SECURITY = 200;

interface LiveState {
  conn: ConnStatus;
  connAttempt: number;
  retryInMs: number | null;
  lastEventAt: number | null;
  ticks: Record<string, Tick>;
  tickMeta: Record<string, TickMeta>;
  bySymbol: Record<string, string>;
  outcomes: OutcomeEvent[];
  outcomesSeeded: boolean;
  mandates: Record<string, MandateView>;
  proposalStatus: Record<string, ProposalStatus>;
  monitor: MonitorStatus | null;
  security: SecurityEvent[];
  lastAudit: { id: number; event_type: string; ts: string } | null;
  setConn: (s: ConnStatus, attempt?: number, retryInMs?: number) => void;
  ingest: (e: LiveEvent) => void;
  seedTicks: (ticks: Tick[]) => void;
  seedOutcomes: (list: OutcomeEvent[]) => void;
}

function applyTick(
  state: Pick<LiveState, "ticks" | "tickMeta" | "bySymbol">,
  t: Tick,
): Pick<LiveState, "ticks" | "tickMeta" | "bySymbol"> {
  const prev = state.ticks[t.instrument_id];
  if (prev && prev.seq > t.seq && prev.ts > t.ts) return state; // out-of-order frame
  const meta = state.tickMeta[t.instrument_id];
  const dir: TickMeta["dir"] = !prev ? 0 : t.ltp_paise > prev.ltp_paise ? 1 : t.ltp_paise < prev.ltp_paise ? -1 : 0;
  return {
    ticks: { ...state.ticks, [t.instrument_id]: t },
    tickMeta: {
      ...state.tickMeta,
      [t.instrument_id]: { dir, n: dir === 0 ? (meta?.n ?? 0) : (meta?.n ?? 0) + 1 },
    },
    bySymbol:
      state.bySymbol[t.symbol] === t.instrument_id
        ? state.bySymbol
        : { ...state.bySymbol, [t.symbol]: t.instrument_id },
  };
}

export const useLiveStore = create<LiveState>()((set) => ({
  conn: "idle",
  connAttempt: 0,
  retryInMs: null,
  lastEventAt: null,
  ticks: {},
  tickMeta: {},
  bySymbol: {},
  outcomes: [],
  outcomesSeeded: false,
  mandates: {},
  proposalStatus: {},
  monitor: null,
  security: [],
  lastAudit: null,
  setConn: (conn, attempt = 0, retryInMs) =>
    set({ conn, connAttempt: attempt, retryInMs: retryInMs ?? null }),
  seedTicks: (ticks) =>
    set((s) => {
      let next: Pick<LiveState, "ticks" | "tickMeta" | "bySymbol"> = s;
      for (const t of ticks) {
        if (!next.ticks[t.instrument_id]) next = applyTick(next, t);
      }
      return next;
    }),
  seedOutcomes: (list) =>
    set((s) => {
      const seen = new Set(s.outcomes.map((o) => o.id));
      const merged = [...s.outcomes, ...list.filter((o) => !seen.has(o.id))];
      merged.sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));
      return { outcomes: merged.slice(0, MAX_OUTCOMES), outcomesSeeded: true };
    }),
  ingest: (e) =>
    set((s) => {
      const lastEventAt = Date.now();
      switch (e.type) {
        case "tick":
          return { ...applyTick(s, e.data), lastEventAt };
        case "outcome":
          if (s.outcomes.some((o) => o.id === e.data.id)) return { lastEventAt };
          return { outcomes: [e.data, ...s.outcomes].slice(0, MAX_OUTCOMES), lastEventAt };
        case "proposal":
          return { proposalStatus: { ...s.proposalStatus, [e.data.id]: e.data.status }, lastEventAt };
        case "mandate":
          return { mandates: { ...s.mandates, [e.data.mandate_id]: e.data }, lastEventAt };
        case "security":
          return { security: [e.data, ...s.security.filter((x) => x.id !== e.data.id)].slice(0, MAX_SECURITY), lastEventAt };
        case "monitor":
          return { monitor: e.data, lastEventAt };
        case "audit":
          return { lastAudit: e.data, lastEventAt };
        case "ping":
        default:
          return { lastEventAt };
      }
    }),
}));

export function useTick(instrumentId: string | null | undefined): Tick | undefined {
  return useLiveStore((s) => (instrumentId ? s.ticks[instrumentId] : undefined));
}

export function useTickMeta(instrumentId: string | null | undefined): TickMeta | undefined {
  return useLiveStore((s) => (instrumentId ? s.tickMeta[instrumentId] : undefined));
}

export function useTickBySymbol(symbol: string | null | undefined): Tick | undefined {
  return useLiveStore((s) => {
    if (!symbol) return undefined;
    const id = s.bySymbol[symbol];
    return id ? s.ticks[id] : undefined;
  });
}

// ---------------------------------------------------------------------------
// UI store: cross-component requests (composer text, palette, presenter panel).

export type CopilotTab = "account" | "market" | "outcomes";

interface UiState {
  composerRequest: { text: string; nonce: number; send: boolean } | null;
  requestComposer: (text: string, opts?: { send?: boolean }) => void;
  consumeComposer: () => void;
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;
  presenterOpen: boolean;
  setPresenterOpen: (v: boolean) => void;
  copilotTab: CopilotTab;
  setCopilotTab: (t: CopilotTab) => void;
  marketSymbol: string;
  setMarketSymbol: (s: string) => void;
  /** Incremented to ask the Attack Lab to run every scenario (from the command palette). */
  attackRunNonce: number;
  requestAttackRun: () => void;
}

export const useUiStore = create<UiState>()((set) => ({
  composerRequest: null,
  requestComposer: (text, opts) =>
    set((s) => ({
      composerRequest: { text, nonce: (s.composerRequest?.nonce ?? 0) + 1, send: !!opts?.send },
    })),
  consumeComposer: () => set({ composerRequest: null }),
  paletteOpen: false,
  setPaletteOpen: (paletteOpen) => set({ paletteOpen }),
  presenterOpen: false,
  setPresenterOpen: (presenterOpen) => set({ presenterOpen }),
  copilotTab: "account",
  setCopilotTab: (copilotTab) => set({ copilotTab }),
  marketSymbol: "RELIANCE",
  setMarketSymbol: (marketSymbol) => set({ marketSymbol }),
  attackRunNonce: 0,
  requestAttackRun: () => set((s) => ({ attackRunNonce: s.attackRunNonce + 1 })),
}));

// ---------------------------------------------------------------------------
// Chat store: our own SSE chat transport (POST /api/chat), not useChat.

export interface ToolActivity {
  id: string;
  name: string;
  label: string;
  status: "running" | "done" | "error";
  detail?: string;
}

export type ChatPart =
  | { type: "text"; text: string }
  | { type: "tool"; tool: ToolActivity }
  | { type: "card"; card: Card; key: string };

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  parts: ChatPart[];
  status: "pending" | "streaming" | "done" | "error" | "stopped";
  error?: { code: string; message: string };
  engine?: string;
  createdAt: string;
}

const CONVO_KEY = "cs_conversation_id";

function storedConversationId(): string | undefined {
  try {
    return typeof window === "undefined" ? undefined : (sessionStorage.getItem(CONVO_KEY) ?? undefined);
  } catch {
    return undefined;
  }
}

function storeConversationId(id: string | undefined) {
  try {
    if (id) sessionStorage.setItem(CONVO_KEY, id);
    else sessionStorage.removeItem(CONVO_KEY);
  } catch {
    // storage unavailable; conversation simply will not survive a reload
  }
}

let msgSeq = 0;
function newId(prefix: string) {
  msgSeq += 1;
  return `${prefix}_${Date.now().toString(36)}_${msgSeq}`;
}

let activeAbort: AbortController | null = null;

interface ChatState {
  messages: ChatMessage[];
  conversationId: string | undefined;
  engine: ChatEngine;
  streaming: boolean;
  historyLoaded: boolean;
  setEngine: (e: ChatEngine) => void;
  send: (text: string) => Promise<void>;
  stop: () => void;
  reset: () => void;
  loadHistory: () => Promise<void>;
  /** Append a system-originated assistant message (e.g. a proposal card fetched from the API). */
  inject: (parts: ChatPart[], text?: string) => void;
}

function applyChatEvent(m: ChatMessage, e: ChatEvent): ChatMessage {
  switch (e.type) {
    case "meta":
      return { ...m, engine: e.engine, status: m.status === "pending" ? "streaming" : m.status };
    case "tool": {
      const idx = m.parts.findIndex((p) => p.type === "tool" && p.tool.id === e.id);
      const tool: ToolActivity = { id: e.id, name: e.name, label: e.label, status: e.status, detail: e.detail };
      const parts = [...m.parts];
      if (idx >= 0) parts[idx] = { type: "tool", tool };
      else parts.push({ type: "tool", tool });
      return { ...m, parts, status: "streaming" };
    }
    case "text-delta": {
      const parts = [...m.parts];
      const last = parts[parts.length - 1];
      if (last && last.type === "text") parts[parts.length - 1] = { type: "text", text: last.text + e.delta };
      else parts.push({ type: "text", text: e.delta });
      return { ...m, parts, status: "streaming" };
    }
    case "card":
      return {
        ...m,
        parts: [...m.parts, { type: "card", card: e.card, key: `${m.id}_card_${m.parts.length}` }],
        status: "streaming",
      };
    case "done":
      return { ...m, status: "done" };
    case "error":
      return { ...m, status: "error", error: { code: e.code, message: e.message } };
    default:
      return m;
  }
}

export const useChatStore = create<ChatState>()((set, get) => ({
  messages: [],
  conversationId: undefined,
  engine: "auto",
  streaming: false,
  historyLoaded: false,
  setEngine: (engine) => set({ engine }),
  stop: () => {
    activeAbort?.abort();
    activeAbort = null;
    set((s) => ({
      streaming: false,
      messages: s.messages.map((m) =>
        m.status === "pending" || m.status === "streaming" ? { ...m, status: "stopped" } : m,
      ),
    }));
  },
  reset: () => {
    activeAbort?.abort();
    activeAbort = null;
    storeConversationId(undefined);
    set({ messages: [], conversationId: undefined, streaming: false });
  },
  inject: (parts, text) => {
    const id = newId("a");
    const all: ChatPart[] = text ? [{ type: "text", text }, ...parts] : parts;
    set((s) => ({
      messages: [
        ...s.messages,
        {
          id,
          role: "assistant",
          status: "done",
          createdAt: new Date().toISOString(),
          parts: all.map((p, i) => (p.type === "card" ? { ...p, key: `${id}_card_${i}` } : p)),
        },
      ],
    }));
  },
  loadHistory: async () => {
    if (get().historyLoaded) return;
    set({ historyLoaded: true });
    const id = storedConversationId();
    if (!id || get().messages.length > 0) return;
    try {
      const items = await api.chatHistory(id);
      if (get().messages.length > 0) return;
      set({
        conversationId: id,
        messages: items.map((it) => ({
          id: it.id,
          role: it.role,
          status: "done",
          createdAt: it.created_at,
          parts: [
            ...(it.text ? [{ type: "text" as const, text: it.text }] : []),
            ...it.cards.map((card, i) => ({ type: "card" as const, card, key: `${it.id}_card_${i}` })),
          ],
        })),
      });
    } catch {
      // History is a convenience; a fresh conversation is fine.
      storeConversationId(undefined);
    }
  },
  send: async (raw) => {
    const text = raw.trim();
    if (!text || get().streaming) return;
    const now = new Date().toISOString();
    const userMsg: ChatMessage = {
      id: newId("u"),
      role: "user",
      parts: [{ type: "text", text }],
      status: "done",
      createdAt: now,
    };
    const asstId = newId("a");
    const asst: ChatMessage = { id: asstId, role: "assistant", parts: [], status: "pending", createdAt: now };
    set((s) => ({ messages: [...s.messages, userMsg, asst], streaming: true }));

    const ac = new AbortController();
    activeAbort = ac;
    const update = (fn: (m: ChatMessage) => ChatMessage) =>
      set((s) => ({ messages: s.messages.map((m) => (m.id === asstId ? fn(m) : m)) }));

    try {
      await api.chat(
        { conversation_id: get().conversationId, message: text, engine: get().engine },
        {
          signal: ac.signal,
          onEvent: (e) => {
            if (e.type === "meta" && e.conversation_id !== get().conversationId) {
              storeConversationId(e.conversation_id);
              set({ conversationId: e.conversation_id });
            }
            update((m) => applyChatEvent(m, e));
          },
        },
      );
      update((m) =>
        m.status === "pending" || m.status === "streaming" ? { ...m, status: ac.signal.aborted ? "stopped" : "done" } : m,
      );
    } catch (err) {
      if (ac.signal.aborted) return;
      update((m) => ({
        ...m,
        status: "error",
        error: { code: isApiError(err) ? err.code : "CHAT_FAILED", message: errorMessage(err) },
      }));
    } finally {
      if (activeAbort === ac) activeAbort = null;
      set({ streaming: false });
    }
  },
}));
