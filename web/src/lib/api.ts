// One typed interface for the Countersign API. The real client talks to /api (proxied to
// FastAPI); the mock client (NEXT_PUBLIC_MOCK=1) serves realistic fixtures in the browser.
// The browser never talks to a broker and never holds credentials: auth is an HttpOnly
// session cookie plus a double-submit CSRF header on every state-changing request.

import type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
} from "@simplewebauthn/browser";
import { ApiError } from "./errors";
import { IS_MOCK } from "./env";
import { mockApi } from "./mock";
import { connectLiveEvents, readSSE, type LiveHandlers } from "./sse";
import type {
  AccountSummary,
  ApiErrorBody,
  ApprovalCompleteBody,
  ApprovalMethod,
  ApprovalStart,
  AttackRun,
  AuditEvent,
  AuditVerify,
  BrokerFault,
  BrokerOrder,
  ChatEvent,
  ChatHistoryItem,
  ChatRequest,
  DemoState,
  Device,
  FeedScenario,
  HistoryPoint,
  Holding,
  InstrumentHandle,
  LoginBody,
  MandateView,
  Me,
  OptionChain,
  OutcomeEvent,
  PinResetBody,
  Position,
  Proposal,
  ProposalKind,
  ProposalStatus,
  PublicStatus,
  Quote,
  Scenario,
  Scoreboard,
  SecurityEvent,
  SecurityEventScope,
  SecurityStatus,
  SessionInfo,
  SignupBody,
  StepUpBody,
  StepUpResult,
} from "./types";

export { ApiError, errorMessage, isApiError } from "./errors";

export { IS_MOCK } from "./env";

export type OkResponse = { ok: true };
export type OrdersFilter = "open" | "all" | "rejected";

export interface PasskeyLoginStart {
  challenge_id: string;
  options: PublicKeyCredentialRequestOptionsJSON;
}

export interface ChatStreamHandlers {
  signal?: AbortSignal;
  onEvent: (e: ChatEvent) => void;
}

export interface CountersignApi {
  // Public (no session)
  publicStatus(): Promise<PublicStatus>;
  // Sign-up and sign-in. Signing in is never a trade approval.
  signup(body: SignupBody): Promise<Me>;
  login(body: LoginBody): Promise<Me>;
  passkeyLoginStart(): Promise<PasskeyLoginStart>;
  passkeyLoginFinish(challenge_id: string, credential: AuthenticationResponseJSON): Promise<Me>;
  // Session and devices
  me(): Promise<Me>;
  logout(): Promise<OkResponse>;
  registerStart(device_label?: string): Promise<PublicKeyCredentialCreationOptionsJSON>;
  registerFinish(body: RegistrationResponseJSON): Promise<Device>;
  setPin(pin: string, current_pin?: string): Promise<OkResponse>;
  pinReset(body: PinResetBody): Promise<OkResponse>;
  /** Re-confirm with an existing approval method before adding or replacing one. Never a trade approval. */
  stepUpStart(): Promise<PasskeyLoginStart>;
  stepUp(body: StepUpBody): Promise<StepUpResult>;
  totpSetup(): Promise<{ otpauth_uri: string; secret: string }>;
  totpVerify(code: string): Promise<OkResponse>;
  revokeDevice(id: string): Promise<OkResponse>;
  sessions(): Promise<SessionInfo[]>;
  revokeSession(id: string): Promise<OkResponse>;
  // Account (read-only gateway)
  accountSummary(): Promise<AccountSummary>;
  holdings(): Promise<Holding[]>;
  positions(): Promise<Position[]>;
  orders(status: OrdersFilter): Promise<BrokerOrder[]>;
  // Market
  instruments(q: string): Promise<InstrumentHandle[]>;
  quote(symbol: string): Promise<Quote>;
  watchlist(): Promise<Quote[]>;
  history(symbol: string, points?: number): Promise<HistoryPoint[]>;
  optionChain(params: { expiry?: string; width?: number }): Promise<OptionChain>;
  // Chat
  chat(req: ChatRequest, handlers: ChatStreamHandlers): Promise<void>;
  chatHistory(conversation_id: string): Promise<ChatHistoryItem[]>;
  // Proposals and countersignature
  proposals(params?: { kind?: ProposalKind; status?: ProposalStatus; limit?: number }): Promise<Proposal[]>;
  proposal(id: string): Promise<Proposal>;
  rejectProposal(id: string): Promise<Proposal>;
  approvalStart(id: string, method: ApprovalMethod): Promise<ApprovalStart>;
  approvalComplete(id: string, body: ApprovalCompleteBody): Promise<Proposal>;
  // Mandates, plans, outcomes
  mandates(): Promise<MandateView[]>;
  cancelMandate(id: string): Promise<MandateView>;
  plans(): Promise<Proposal[]>;
  outcomes(limit?: number): Promise<OutcomeEvent[]>;
  // Audit and security
  audit(params?: { limit?: number; before_id?: number }): Promise<AuditEvent[]>;
  auditVerify(): Promise<AuditVerify>;
  securityStatus(): Promise<SecurityStatus>;
  securityEvents(scope?: SecurityEventScope): Promise<SecurityEvent[]>;
  killSwitch(engaged: boolean, pin?: string): Promise<SecurityStatus>;
  // Attack Lab
  scenarios(): Promise<Scenario[]>;
  runAttack(scenario_id: string): Promise<AttackRun>;
  attackRuns(limit?: number): Promise<AttackRun[]>;
  scoreboard(): Promise<Scoreboard>;
  // Demo controls
  demoState(): Promise<DemoState>;
  demoReset(): Promise<unknown>;
  demoFeed(scenario: FeedScenario, symbol?: string): Promise<unknown>;
  demoBrokerFault(mode: BrokerFault, count?: number): Promise<unknown>;
  // Live events
  subscribe(handlers: LiveHandlers): () => void;
}

// ---------------------------------------------------------------------------
// Real client

export const CHAT_STREAM_URL = "/api-stream/chat";

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const parts = document.cookie ? document.cookie.split("; ") : [];
  for (const p of parts) {
    const eq = p.indexOf("=");
    if (eq > 0 && p.slice(0, eq) === name) return decodeURIComponent(p.slice(eq + 1));
  }
  return null;
}

function stateChangingHeaders(): Record<string, string> {
  return { "X-CSRF-Token": readCookie("cs_csrf") ?? "" };
}

async function toApiError(res: Response, method = "GET"): Promise<ApiError> {
  const status = res.status;
  let text = "";
  try {
    text = await res.text();
  } catch {
    // ignore
  }
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (body && typeof body === "object") {
    const b = body as Partial<ApiErrorBody> & { detail?: unknown };
    if (typeof b.code === "string" && typeof b.message === "string") {
      const { code, message, detail, ...extra } = b as ApiErrorBody & Record<string, unknown>;
      return new ApiError(status, code, message, detail, extra);
    }
    // FastAPI default shapes: { detail: string } or { detail: [{ msg, loc }] }
    if (typeof b.detail === "string") return new ApiError(status, `HTTP_${status}`, b.detail);
    if (b.detail && typeof b.detail === "object" && "code" in (b.detail as object)) {
      const d = b.detail as Partial<ApiErrorBody>;
      if (typeof d.code === "string" && typeof d.message === "string") {
        const { code, message, detail, ...extra } = d as ApiErrorBody & Record<string, unknown>;
        return new ApiError(status, code, message, detail, extra);
      }
    }
    if (Array.isArray(b.detail)) {
      const msgs = (b.detail as { msg?: string; loc?: unknown[] }[])
        .map((d) => [d.loc?.slice(1).join("."), d.msg].filter(Boolean).join(": "))
        .join("; ");
      return new ApiError(status, "VALIDATION_ERROR", `The request was rejected as invalid: ${msgs}`);
    }
  }
  if (status === 401) {
    return new ApiError(401, "UNAUTHENTICATED", "Your session has ended. Sign in again to continue.");
  }
  if (status === 403) {
    return new ApiError(403, "FORBIDDEN", "The API refused this request (missing or stale CSRF token). Reload the page and try again.");
  }
  const where = `${method} ${res.url.replace(/^https?:\/\/[^/]+/, "")}`;
  if (status === 502 || status === 503 || status === 504) {
    return new ApiError(
      status,
      "BACKEND_UNAVAILABLE",
      `The Countersign API is not reachable (HTTP ${status} on ${where}). Start the backend on port 8000, or run the UI with NEXT_PUBLIC_MOCK=1.`,
    );
  }
  if (status >= 500) {
    return new ApiError(
      status,
      "BACKEND_ERROR",
      `The Countersign API failed with an internal error (HTTP ${status}) on ${where}. Check the backend logs; if it is not running, start it on port 8000 or use NEXT_PUBLIC_MOCK=1.`,
    );
  }
  return new ApiError(status, `HTTP_${status}`, `The API returned HTTP ${status} for ${where}.`);
}

async function request<T>(method: "GET" | "POST" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") Object.assign(headers, stateChangingHeaders());
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      credentials: "include",
      cache: "no-store",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(
      0,
      "NETWORK_ERROR",
      "Cannot reach the Countersign web server. Check that `pnpm dev` is running and your network is up.",
    );
  }
  if (!res.ok) throw await toApiError(res, method);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

const enc = encodeURIComponent;

export const realApi: CountersignApi = {
  publicStatus: () => request("GET", "/api/public/status"),
  signup: (body) => request("POST", "/api/auth/signup", body),
  login: (body) => request("POST", "/api/auth/login", body),
  passkeyLoginStart: () => request("POST", "/api/auth/passkey/login/start", {}),
  passkeyLoginFinish: (challenge_id, credential) =>
    request("POST", "/api/auth/passkey/login/finish", { challenge_id, credential }),
  me: () => request("GET", "/api/me"),
  logout: () => request("POST", "/api/auth/logout"),
  registerStart: (device_label) =>
    request("POST", "/api/auth/webauthn/register/start", device_label ? { device_label } : {}),
  registerFinish: (body) => request("POST", "/api/auth/webauthn/register/finish", body),
  setPin: (pin, current_pin) => request("POST", "/api/auth/pin", current_pin ? { pin, current_pin } : { pin }),
  pinReset: (body) => request("POST", "/api/auth/pin/reset", body),
  stepUpStart: () => request("POST", "/api/auth/step-up/start", {}),
  stepUp: (body) => request("POST", "/api/auth/step-up", body),
  totpSetup: () => request("POST", "/api/auth/totp/setup"),
  totpVerify: (code) => request("POST", "/api/auth/totp/verify", { code }),
  revokeDevice: (id) => request("DELETE", `/api/auth/devices/${enc(id)}`),
  sessions: () => request("GET", "/api/sessions"),
  revokeSession: (id) => request("DELETE", `/api/sessions/${enc(id)}`),

  accountSummary: () => request("GET", "/api/account/summary"),
  holdings: () => request("GET", "/api/account/holdings"),
  positions: () => request("GET", "/api/account/positions"),
  orders: (status) => request("GET", `/api/account/orders${qs({ status })}`),

  instruments: (q) => request("GET", `/api/market/instruments${qs({ q })}`),
  quote: (symbol) => request("GET", `/api/market/quote/${enc(symbol)}`),
  watchlist: () => request("GET", "/api/market/watchlist"),
  history: (symbol, points = 300) => request("GET", `/api/market/history/${enc(symbol)}${qs({ points })}`),
  optionChain: ({ expiry, width = 8 }) =>
    request("GET", `/api/market/option-chain${qs({ underlying: "NIFTY", expiry, width })}`),

  async chat(req, { signal, onEvent }) {
    let res: Response;
    try {
      res = await fetch(CHAT_STREAM_URL, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          ...stateChangingHeaders(),
        },
        body: JSON.stringify(req),
        signal,
      });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      throw new ApiError(0, "NETWORK_ERROR", "Cannot reach the Countersign web server to stream the reply.");
    }
    if (!res.ok || !res.body) throw await toApiError(res, "POST");
    for await (const msg of readSSE(res.body, signal)) {
      if (!msg.data) continue;
      let evt: ChatEvent;
      try {
        evt = JSON.parse(msg.data) as ChatEvent;
      } catch {
        continue;
      }
      onEvent(evt);
      if (evt.type === "done" || evt.type === "error") break;
    }
  },
  chatHistory: (conversation_id) => request("GET", `/api/chat/history${qs({ conversation_id })}`),

  proposals: (params = {}) => request("GET", `/api/proposals${qs(params)}`),
  proposal: (id) => request("GET", `/api/proposals/${enc(id)}`),
  rejectProposal: (id) => request("POST", `/api/proposals/${enc(id)}/reject`),
  approvalStart: (id, method) => request("POST", `/api/proposals/${enc(id)}/approval/start`, { method }),
  approvalComplete: (id, body) => request("POST", `/api/proposals/${enc(id)}/approval/complete`, body),

  mandates: () => request("GET", "/api/mandates"),
  cancelMandate: (id) => request("POST", `/api/mandates/${enc(id)}/cancel`),
  plans: () => request("GET", "/api/plans"),
  outcomes: (limit = 50) => request("GET", `/api/outcomes${qs({ limit })}`),

  audit: (params = {}) => request("GET", `/api/audit${qs({ limit: params.limit ?? 100, before_id: params.before_id })}`),
  auditVerify: () => request("GET", "/api/audit/verify"),
  securityStatus: () => request("GET", "/api/security/status"),
  securityEvents: (scope = "mine") => request("GET", `/api/security/events${qs({ scope })}`),
  killSwitch: (engaged, pin) => request("POST", "/api/security/kill-switch", pin ? { engaged, pin } : { engaged }),

  scenarios: () => request("GET", "/api/attack-lab/scenarios"),
  runAttack: (scenario_id) => request("POST", "/api/attack-lab/run", { scenario_id }),
  attackRuns: (limit = 10) => request("GET", `/api/attack-lab/runs${qs({ limit })}`),
  scoreboard: () => request("GET", "/api/attack-lab/scoreboard"),

  demoState: () => request("GET", "/api/demo/state"),
  demoReset: () => request("POST", "/api/demo/reset"),
  demoFeed: (scenario, symbol) => request("POST", "/api/demo/feed", symbol ? { scenario, symbol } : { scenario }),
  demoBrokerFault: (mode, count) => request("POST", "/api/demo/broker-fault", count ? { mode, count } : { mode }),

  subscribe: (handlers) => connectLiveEvents(handlers),
};

export const api: CountersignApi = IS_MOCK ? mockApi : realApi;
