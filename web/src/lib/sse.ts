// Server-Sent Events helpers.
// 1. readSSE: parse a fetch() response body (used for POST /api/chat streaming).
// 2. connectLiveEvents: one shared EventSource to GET /api/events with backoff reconnect.

import type { LiveEvent } from "./types";

export interface SSEMessage {
  event: string;
  data: string;
  id?: string;
}

/** Incrementally parse an SSE byte stream per the WHATWG spec (data, event, id, comments). */
export async function* readSSE(
  stream: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SSEMessage> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let data: string[] = [];
  let id: string | undefined;

  const flush = (): SSEMessage | null => {
    if (data.length === 0) {
      event = "message";
      return null;
    }
    const msg: SSEMessage = { event, data: data.join("\n"), id };
    event = "message";
    data = [];
    return msg;
  };

  try {
    while (true) {
      if (signal?.aborted) break;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      // Split on any line terminator, keep the trailing partial line in the buffer.
      while ((idx = buffer.search(/\r\n|\r|\n/)) !== -1) {
        const line = buffer.slice(0, idx);
        const sepLen = buffer.startsWith("\r\n", idx) ? 2 : 1;
        buffer = buffer.slice(idx + sepLen);
        if (line === "") {
          const msg = flush();
          if (msg) yield msg;
          continue;
        }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let val = colon === -1 ? "" : line.slice(colon + 1);
        if (val.startsWith(" ")) val = val.slice(1);
        if (field === "data") data.push(val);
        else if (field === "event") event = val || "message";
        else if (field === "id") id = val;
      }
    }
    const tail = flush();
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}

export type ConnStatus = "idle" | "connecting" | "open" | "reconnecting" | "offline";

export const LIVE_EVENT_TYPES = [
  "tick",
  "outcome",
  "proposal",
  "mandate",
  "security",
  "monitor",
  "audit",
  "ping",
] as const;

export interface LiveHandlers {
  onEvent: (e: LiveEvent) => void;
  onStatus: (s: ConnStatus, detail?: { attempt: number; retryInMs?: number }) => void;
}

/** Path of the live events stream. Proxied by a route handler so it is never buffered. */
export const EVENTS_URL = "/api-stream/events";

/**
 * Open the shared EventSource with exponential backoff (1 s, 2 s, 4 s ... max 20 s, with jitter).
 * Returns a disposer.
 */
export function connectLiveEvents(handlers: LiveHandlers): () => void {
  let es: EventSource | null = null;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let disposed = false;

  const dispatch = (type: LiveEvent["type"], raw: string) => {
    if (type === "ping") {
      handlers.onEvent({ type: "ping", data: null });
      return;
    }
    try {
      const data = JSON.parse(raw);
      handlers.onEvent({ type, data } as LiveEvent);
    } catch {
      // Ignore malformed frames; the stream stays up.
    }
  };

  const open = () => {
    if (disposed) return;
    handlers.onStatus(attempt === 0 ? "connecting" : "reconnecting", { attempt });
    es = new EventSource(EVENTS_URL, { withCredentials: true });
    es.onopen = () => {
      attempt = 0;
      handlers.onStatus("open", { attempt: 0 });
    };
    for (const type of LIVE_EVENT_TYPES) {
      es.addEventListener(type, (ev) => dispatch(type, (ev as MessageEvent<string>).data));
    }
    // Unnamed frames: accept `{ "type": "...", "data": ... }` envelopes as a fallback.
    es.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as { type?: string; data?: unknown };
        if (parsed.type && (LIVE_EVENT_TYPES as readonly string[]).includes(parsed.type)) {
          handlers.onEvent({ type: parsed.type, data: parsed.data } as LiveEvent);
        }
      } catch {
        // ignore
      }
    };
    es.onerror = () => {
      es?.close();
      es = null;
      if (disposed) return;
      attempt += 1;
      const base = Math.min(20_000, 1000 * 2 ** Math.min(attempt - 1, 5));
      const retryInMs = Math.round(base * (0.8 + Math.random() * 0.4));
      handlers.onStatus(attempt > 3 ? "offline" : "reconnecting", { attempt, retryInMs });
      timer = setTimeout(open, retryInMs);
    };
  };

  open();

  return () => {
    disposed = true;
    if (timer) clearTimeout(timer);
    es?.close();
    es = null;
    handlers.onStatus("idle", { attempt: 0 });
  };
}
