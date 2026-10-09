// In-browser mock backend. Mutable state plus simulations of the feed, the approval
// ceremony, the executor and the mandate engine, so every screen is demoable offline.

import { ApiError } from "../errors";
import type {
  AttackRun,
  AuditEvent,
  BrokerFault,
  BrokerOrder,
  Device,
  ExecutionState,
  FeedScenario,
  LiveEvent,
  MandateView,
  OutcomeEvent,
  Proposal,
  ProposalStatus,
  ProposalStep,
  Scoreboard,
  SecurityEvent,
  SecurityStatus,
  SessionInfo,
  StepState,
  Tick,
} from "../types";
import {
  ACCOUNT_ID,
  BASE,
  HOLDING_SEEDS,
  INSTRUMENTS,
  OPTION_POSITION,
  SCENARIOS,
  WATCHLIST,
  buildHolding,
  buildPosition,
  buildStep,
  buildSummary,
  fakeHash,
  finalizeProposal,
  isoAgo,
  isoIn,
  makeTick,
  mandateView,
  roundToTick,
  scenarioResult,
  seedAudit,
  seedOrders,
  seedOutcomes,
  seedSecurityEvents,
  seedSecurityStatus,
  verifiedApproval,
  type HoldingSeed,
  type OrderSpec,
  type PositionSeed,
} from "./fixtures";
import { findUser, upsertUser, type MockUser } from "./users";

// Mock authenticator apps always accept this code. The app PIN is whatever the user chose.
export const MOCK_TOTP = "123456";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });

interface PriceState {
  ltp: number;
  prev: number;
  vol: number;
  target: number;
}

interface PendingApproval {
  approval_id: string;
  proposal_id: string;
  method: "webauthn" | "pin" | "totp";
  issued_at: number;
  expires_at: number;
  payload_hash: string;
  used: boolean;
}

interface State {
  loggedIn: boolean;
  /** Signed-in mock user; their PIN, passkeys and TOTP are mirrored into the fields below. */
  userId: string | null;
  devices: Device[];
  hasPin: boolean;
  pin: string;
  pinFails: number;
  pinLockedUntil: number | null;
  hasTotp: boolean;
  sessions: SessionInfo[];
  prices: Record<string, PriceState>;
  optionLtp: number;
  optionPrev: number;
  seq: number;
  holdings: HoldingSeed[];
  cash: number;
  usedMargin: number;
  positions: PositionSeed[];
  orders: BrokerOrder[];
  proposals: Map<string, Proposal>;
  approvals: Map<string, PendingApproval>;
  outcomes: OutcomeEvent[];
  security: SecurityStatus;
  securityEvents: SecurityEvent[];
  audit: AuditEvent[];
  feedMode: FeedScenario;
  feedSymbol: string;
  staleSince: number | null;
  brokerFault: BrokerFault;
  faultCount: number;
  scoreboard: Scoreboard;
  runs: AttackRun[];
  pendingDeviceLabel: string;
}

const LOGIN_KEY = "cs_mock_logged_in";
const STATE_KEY = "cs_mock_state_v1";

function readLoggedIn(): boolean {
  try {
    return typeof window !== "undefined" && sessionStorage.getItem(LOGIN_KEY) === "1";
  } catch {
    return false;
  }
}

function writeLoggedIn(v: boolean) {
  try {
    if (v) sessionStorage.setItem(LOGIN_KEY, "1");
    else sessionStorage.removeItem(LOGIN_KEY);
  } catch {
    // ignore
  }
}

type SerializedState = Omit<State, "proposals" | "approvals"> & {
  proposals: [string, Proposal][];
  approvals: [string, PendingApproval][];
};

function saveState(s: State) {
  try {
    const data: SerializedState = { ...s, proposals: [...s.proposals.entries()], approvals: [...s.approvals.entries()] };
    sessionStorage.setItem(STATE_KEY, JSON.stringify(data));
  } catch {
    // storage full or unavailable: the demo simply will not survive a reload
  }
}

function loadState(): State | null {
  try {
    const raw = typeof window === "undefined" ? null : sessionStorage.getItem(STATE_KEY);
    if (!raw) return null;
    const d = JSON.parse(raw) as SerializedState;
    return { ...d, proposals: new Map(d.proposals), approvals: new Map(d.approvals) };
  } catch {
    return null;
  }
}

function clearState() {
  try {
    sessionStorage.removeItem(STATE_KEY);
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Seed proposals

function heldQty(s: State, symbol: string) {
  return s.holdings.find((h) => h.symbol === symbol)?.qty ?? 0;
}

function summaryOf(s: State) {
  const holdings = s.holdings.map((h) => buildHolding(h, s.prices[h.symbol].ltp, s.prices[h.symbol].prev));
  const positions = s.positions.map((p) =>
    buildPosition(p, p.inst.instrument_id === OPTION_POSITION.instrument_id ? s.optionLtp : (s.prices[p.inst.symbol]?.ltp ?? p.avg)),
  );
  return buildSummary(
    holdings,
    positions,
    s.cash,
    s.usedMargin,
    s.orders.filter((o) => o.status === "OPEN" || o.status === "PARTIALLY_FILLED" || o.status === "TRIGGER_PENDING").length,
  );
}

function specFor(s: State, partial: Omit<OrderSpec, "ltp" | "heldQty" | "availableMargin" | "exposure">): OrderSpec {
  const sum = summaryOf(s);
  return {
    ...partial,
    ltp: s.prices[partial.symbol]?.ltp ?? BASE[partial.symbol].ltp,
    heldQty: heldQty(s, partial.symbol),
    availableMargin: sum.available_margin_paise,
    exposure: sum.exposure_paise,
  };
}

function baseProposal(
  id: string,
  kind: Proposal["kind"],
  status: ProposalStatus,
  userText: string,
  engine: string,
  steps: ProposalStep[],
  createdAgo: number,
): Omit<Proposal, "canonical_hash" | "short_hash"> {
  const issues = steps.flatMap((st) => st.issues);
  return {
    id,
    kind,
    status,
    summary:
      kind === "PLAN"
        ? `PLAN (${steps.length} steps): ${steps.map((st) => st.summary).join(", then ")}`
        : (steps[0]?.summary ?? ""),
    steps,
    mandate: null,
    failure_policy: kind === "PLAN" ? "STOP_ON_FAILURE" : null,
    issues,
    can_approve: status === "AWAITING_APPROVAL" && !issues.some((i) => i.severity === "BLOCK"),
    interpretation: { user_text: userText, engine, disagreements: [] },
    approval: null,
    execution: null,
    created_at: isoAgo(createdAgo),
    updated_at: isoAgo(createdAgo),
  };
}

function seedProposals(s: State): Map<string, Proposal> {
  const map = new Map<string, Proposal>();
  const add = (p: Proposal) => map.set(p.id, p);

  // Historical: completed order with a device-signed approval
  {
    const step = buildStep(specFor(s, { side: "BUY", symbol: "ITC", qty: 25, orderType: "LIMIT", limit: 46500, userText: "Buy 25 ITC at 465" }), 1, null);
    step.state = "FILLED";
    step.broker_order_id = "PB-260108-000412";
    step.client_order_id = "CS-7c1e4a9b20-1";
    step.filled_quantity = 25;
    step.avg_fill_price_paise = 46495;
    const p = finalizeProposal({
      ...baseProposal("prp_hist_itc", "ORDER", "COMPLETED", "Buy 25 ITC at 465", "ollama:qwen3:4b-instruct-2507", [step], 26 * HOUR + 30_000),
    });
    p.approval = verifiedApproval("webauthn", p.canonical_hash, Date.now() - 26 * HOUR - 10_000);
    p.execution = {
      job_id: "job_hist_itc",
      status: "DONE",
      reason: null,
      outbox: [{ client_order_id: "CS-7c1e4a9b20-1", step_no: 1, state: "FILLED", broker_order_id: "PB-260108-000412", attempts: 1, reconciled: false }],
    };
    add(p);
  }
  // Historical: rejected by user
  {
    const step = buildStep(specFor(s, { side: "SELL", symbol: "HDFCBANK", qty: 10, orderType: "LIMIT", limit: 170000, userText: "Sell 10 HDFCBANK at 1700" }), 1, null);
    add(finalizeProposal(baseProposal("prp_hist_hdfc", "ORDER", "REJECTED_BY_USER", "Sell 10 HDFCBANK at 1700", "gemini:gemini-2.5-flash", [step], 5 * HOUR)));
  }
  // Historical: expired approval window
  {
    const step = buildStep(specFor(s, { side: "BUY", symbol: "TCS", qty: 5, orderType: "LIMIT", limit: 340000, userText: "Buy 5 TCS at 3400" }), 1, null);
    const p = finalizeProposal(baseProposal("prp_hist_tcs", "ORDER", "EXPIRED", "Buy 5 TCS at 3400", "deterministic", [step], 3 * HOUR));
    p.approval = { ...verifiedApproval("pin", p.canonical_hash, Date.now() - 3 * HOUR), status: "EXPIRED", verified_at: null, failure_reason: "Approval window (60 s) elapsed before the PIN was entered." };
    add(p);
  }
  // NEEDS_REVIEW mandate
  {
    const step = buildStep(specFor(s, { side: "SELL", symbol: "SBIN", qty: 50, orderType: "LIMIT", limit: 83000, userText: "Sell 50 SBIN if it rises above 830" }), 1, null);
    const p = finalizeProposal({
      ...baseProposal("prp_mandate_sbin", "MANDATE", "ACTIVE", "Sell 50 SBIN if it rises above 830", "deterministic", [step], 2 * DAY),
      summary: "WHEN LTP >= ₹830.00: SELL 50 SBIN @ ₹830.00 LIMIT (CNC, DAY)",
    });
    p.mandate = mandateView("mnd_sbin_830", INSTRUMENTS.SBIN, {
      kind: "ORDER",
      op: "GTE",
      threshold: 83000,
      template: step.canonical,
      state: "NEEDS_REVIEW",
      createdAgo: 2 * DAY,
      expiresIn: 3 * DAY,
      reason: "Price crossed ₹830.00 at 09:41:12 while the system was offline; it will not fire stale. Review and countersign again.",
      proposal_id: p.id,
    });
    p.approval = verifiedApproval("webauthn", p.canonical_hash, Date.now() - 2 * DAY);
    add(p);
  }
  // Fired alert
  {
    const p = finalizeProposal({
      ...baseProposal("prp_alert_itc", "MANDATE", "COMPLETED", "Alert me if ITC falls below 450", "deterministic", [], 2 * DAY + HOUR),
      summary: "ALERT WHEN LTP <= ₹450.00 on ITC",
    });
    p.mandate = mandateView("mnd_itc_alert", INSTRUMENTS.ITC, {
      kind: "ALERT",
      op: "LTE",
      threshold: 45000,
      template: null,
      state: "FIRED",
      createdAgo: 2 * DAY + HOUR,
      expiresIn: 5 * DAY,
      firedAgo: 30 * HOUR,
      proposal_id: p.id,
    });
    p.approval = verifiedApproval("pin", p.canonical_hash, Date.now() - 2 * DAY - HOUR + 30_000);
    add(p);
  }
  // ACTIVE mandate: TATAMOTORS
  {
    const step = buildStep(specFor(s, { side: "BUY", symbol: "TATAMOTORS", qty: 100, orderType: "LIMIT", limit: 90000, userText: "Buy 100 TATAMOTORS if price falls below 900" }), 1, null);
    const p = finalizeProposal({
      ...baseProposal("prp_mandate_tata", "MANDATE", "ACTIVE", "Buy 100 TATAMOTORS if price falls below 900", "ollama:qwen3:4b-instruct-2507", [step], 3 * MIN + 40_000),
      summary: "WHEN LTP <= ₹900.00: BUY 100 TATAMOTORS @ ₹900.00 LIMIT (CNC, DAY)",
    });
    p.mandate = mandateView("mnd_tata_900", INSTRUMENTS.TATAMOTORS, {
      kind: "ORDER",
      op: "LTE",
      threshold: 90000,
      template: step.canonical,
      state: "ACTIVE",
      createdAgo: 3 * MIN + 40_000,
      expiresIn: 5 * DAY,
      proposal_id: p.id,
    });
    p.approval = verifiedApproval("webauthn", p.canonical_hash, Date.now() - 3 * MIN);
    add(p);
  }
  // BLOCKED: AI misread 100 as 1000
  {
    const step = buildStep(
      specFor(s, { side: "BUY", symbol: "SBIN", qty: 100, modelQty: 1000, orderType: "MARKET", userText: "Buy 100 SBIN at market" }),
      1,
      null,
    );
    const p = finalizeProposal(baseProposal("prp_blocked_sbin", "ORDER", "BLOCKED", "Buy 100 SBIN at market", "ollama:qwen3:4b-instruct-2507", [step], 2 * MIN));
    p.interpretation.disagreements = [{ field: "quantity", trader_value: "100", model_value: "1000" }];
    p.can_approve = false;
    add(p);
  }
  // 2-step PLAN awaiting approval
  {
    const s1 = buildStep(specFor(s, { side: "SELL", symbol: "INFY", qty: 20, orderType: "LIMIT", limit: 153000, userText: "Sell 20 INFY and buy 5 TCS" }), 1, null);
    const s2 = buildStep(specFor(s, { side: "BUY", symbol: "TCS", qty: 5, orderType: "LIMIT", limit: 341500, userText: "Sell 20 INFY and buy 5 TCS" }), 2, 1);
    add(finalizeProposal(baseProposal("prp_plan_infy_tcs", "PLAN", "AWAITING_APPROVAL", "Sell 20 INFY and buy 5 TCS", "gemini:gemini-2.5-flash", [s1, s2], 70_000)));
  }
  // ORDER awaiting approval
  {
    const step = buildStep(specFor(s, { side: "BUY", symbol: "RELIANCE", qty: 10, orderType: "LIMIT", limit: 280000, userText: "Buy 10 RELIANCE at 2800" }), 1, null);
    add(finalizeProposal(baseProposal("prp_await_rel", "ORDER", "AWAITING_APPROVAL", "Buy 10 RELIANCE at 2800", "deterministic", [step], 40_000)));
  }
  return map;
}

function initialState(): State {
  const prices: Record<string, PriceState> = {};
  for (const [sym, b] of Object.entries(BASE)) prices[sym] = { ltp: b.ltp, prev: b.prev, vol: b.vol, target: b.ltp };
  const s: State = {
    loggedIn: false,
    userId: null,
    devices: [],
    hasPin: false,
    pin: "",
    pinFails: 0,
    pinLockedUntil: null,
    hasTotp: false,
    sessions: [],
    prices,
    optionLtp: 15620,
    optionPrev: 13890,
    seq: 1000,
    holdings: HOLDING_SEEDS.map((h) => ({ ...h })),
    cash: 48_235_075,
    usedMargin: 11_286_000,
    positions: [
      { inst: OPTION_POSITION, product: "NRML", qty: 65, avg: 14250, prev: 13890 },
      { inst: INSTRUMENTS.SBIN, product: "MIS", qty: -50, avg: 81500, prev: 80890 },
    ],
    orders: seedOrders(),
    proposals: new Map(),
    approvals: new Map(),
    outcomes: seedOutcomes(),
    security: seedSecurityStatus(),
    securityEvents: seedSecurityEvents(),
    audit: seedAudit(),
    feedMode: "calm",
    feedSymbol: "TATAMOTORS",
    staleSince: null,
    brokerFault: "none",
    faultCount: 0,
    scoreboard: { unapproved_executions: 0, naive_unapproved_executions: 11, attacks_attempted: 18, attacks_blocked: 18, last_run_at: isoAgo(2 * HOUR) },
    runs: [],
    pendingDeviceLabel: "This device",
  };
  s.proposals = seedProposals(s);
  s.runs = [makeRun(SCENARIOS.map((sc) => sc.id), Date.now() - 2 * HOUR, s.scoreboard)];
  return s;
}

function makeRun(ids: string[], at: number, scoreboard: Scoreboard): AttackRun {
  const results = SCENARIOS.filter((sc) => ids.includes(sc.id)).map(scenarioResult);
  const dur = results.reduce((a, r) => a + r.duration_ms, 0);
  return {
    id: `run_${fakeHash(String(at)).slice(0, 10)}`,
    started_at: new Date(at - dur).toISOString(),
    finished_at: new Date(at).toISOString(),
    results,
    scoreboard,
  };
}

// ---------------------------------------------------------------------------
// Engine

type Listener = (e: LiveEvent) => void;

class MockEngine {
  s: State = initialState();
  private listeners = new Set<Listener>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private loginRestored = false;
  private saveTimer: ReturnType<typeof setTimeout> | null = null;

  /** Restore the session flag and any persisted demo state (client only, once). */
  ensureLoginRestored() {
    if (this.loginRestored) return;
    this.loginRestored = true;
    const saved = loadState();
    if (saved) {
      this.s = saved;
      // Heartbeats saved before a reload are old news; the simulated processes are alive now.
      const now = new Date().toISOString();
      this.s.security.executor = { ...this.s.security.executor, last_heartbeat: now };
      this.s.security.monitor = { ...this.s.security.monitor, last_heartbeat: now };
      // A reload mid-execution resumes the executor; filled steps are never re-sent.
      for (const p of saved.proposals.values()) {
        if (p.status === "APPROVED" || p.status === "EXECUTING") setTimeout(() => void this.execute(p.id, {}), 400);
      }
    }
    this.s.loggedIn = readLoggedIn();
  }

  /** Persist the demo state so a browser refresh does not lose the story (throttled). */
  private scheduleSave() {
    if (typeof window === "undefined" || this.saveTimer) return;
    this.saveTimer = setTimeout(() => {
      this.saveTimer = null;
      saveState(this.s);
    }, 800);
  }

  requireAuth() {
    this.ensureLoginRestored();
    if (!this.s.loggedIn || !this.s.userId) {
      throw new ApiError(401, "UNAUTHENTICATED", "You are not signed in. Sign in to continue.");
    }
  }

  /** Start a session for a mock user. A different user gets a fresh copy of the demo portfolio. */
  signIn(u: MockUser) {
    this.ensureLoginRestored();
    if (this.s.userId !== u.id) {
      clearState();
      this.s = initialState();
    }
    const now = new Date();
    this.s.userId = u.id;
    this.s.pin = u.pin ?? "";
    this.s.hasPin = !!u.pin;
    this.s.hasTotp = u.has_totp;
    this.s.devices = u.devices;
    this.s.pinFails = u.pin_fails;
    this.s.pinLockedUntil = u.pin_locked_until;
    this.s.sessions = [
      {
        id: "ses_current",
        created_at: now.toISOString(),
        expires_at: new Date(now.getTime() + 8 * HOUR).toISOString(),
        user_agent: "This browser",
        current: true,
        revoked: false,
      },
    ];
    this.setLoggedIn(true);
    this.audit(`user:${u.id}`, "SESSION_CREATED", "session", "ses_current");
  }

  /** Copy the signed-in user's credential state back to the mock account store. */
  syncUser() {
    const u = findUser(this.s.userId);
    if (!u) return;
    upsertUser({
      ...u,
      pin: this.s.hasPin ? this.s.pin : null,
      has_totp: this.s.hasTotp,
      devices: this.s.devices,
      pin_fails: this.s.pinFails,
      pin_locked_until: this.s.pinLockedUntil,
    });
  }

  setLoggedIn(v: boolean) {
    this.ensureLoginRestored();
    this.s.loggedIn = v;
    writeLoggedIn(v);
    this.scheduleSave();
  }

  reset() {
    const { loggedIn, userId, devices, hasPin, pin, hasTotp, sessions } = this.s;
    clearState();
    this.s = initialState();
    // Demo reset restores the paper account, never the user's own credentials.
    Object.assign(this.s, { loggedIn, userId, devices, hasPin, pin, hasTotp, sessions });
    this.audit("user:demo", "DEMO_RESET", "demo", "demo");
  }

  // ---- event bus
  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    this.start();
    return () => {
      this.listeners.delete(fn);
      if (this.listeners.size === 0) this.stop();
    };
  }

  emit(e: LiveEvent) {
    if (e.type !== "tick" && e.type !== "ping") this.scheduleSave();
    for (const l of this.listeners) {
      try {
        l(e);
      } catch {
        // listener errors must not break the bus
      }
    }
  }

  private start() {
    if (this.timer || typeof window === "undefined") return;
    this.timer = setInterval(() => this.step(), 420);
    this.heartbeat = setInterval(() => this.beat(), 4000);
  }

  private stop() {
    if (this.timer) clearInterval(this.timer);
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.timer = null;
    this.heartbeat = null;
  }

  // ---- feed simulation
  tickFor(symbol: string, stale = false): Tick {
    const p = this.s.prices[symbol];
    this.s.seq += 1;
    return makeTick(INSTRUMENTS[symbol], p.ltp, p.prev, p.vol, this.s.seq, stale);
  }

  optionTick(): Tick {
    this.s.seq += 1;
    return makeTick(OPTION_POSITION, this.s.optionLtp, this.s.optionPrev, 1_284_350, this.s.seq);
  }

  allTicks(): Tick[] {
    return [...WATCHLIST.map((sym) => this.tickFor(sym, this.s.feedMode === "stale")), this.optionTick()];
  }

  private step() {
    const s = this.s;
    if (s.feedMode === "stale") {
      const age = Date.now() - (s.staleSince ?? Date.now());
      s.security.feed = { ...s.security.feed, healthy: false, last_tick_age_ms: age };
      return;
    }
    const n = 3;
    const picks = new Set<string>(["NIFTY"]);
    while (picks.size < n + 1) picks.add(WATCHLIST[Math.floor(Math.random() * WATCHLIST.length)]);
    for (const sym of picks) {
      const p = s.prices[sym];
      const pull = (p.target - p.ltp) * (s.feedMode === "calm" ? 0.08 : 0.22);
      const noise = (Math.random() - 0.5) * p.ltp * 0.0009;
      const next = roundToTick(Math.round(p.ltp + pull + noise));
      const oldNifty = s.prices.NIFTY.ltp;
      p.ltp = Math.max(5, next);
      p.vol += Math.round(Math.random() * 900);
      this.emit({ type: "tick", data: this.tickFor(sym) });
      if (sym === "NIFTY") {
        const delta = (p.ltp - oldNifty) * 0.55;
        s.optionLtp = Math.max(5, roundToTick(Math.round(s.optionLtp + delta + (Math.random() - 0.5) * 30)));
        this.emit({ type: "tick", data: this.optionTick() });
      }
      this.evaluateMandates(sym);
    }
    s.security.feed = { ...s.security.feed, healthy: true, last_tick_age_ms: 180 + Math.round(Math.random() * 300) };
  }

  private beat() {
    const s = this.s;
    const now = new Date().toISOString();
    s.security.monitor = {
      ...s.security.monitor,
      last_heartbeat: now,
      checks: s.security.monitor.checks.map((c) => ({ ...c, checked: c.checked + 1 + Math.floor(Math.random() * 3) })),
    };
    s.security.executor = { ...s.security.executor, last_heartbeat: now };
    s.security.broker = { ...s.security.broker, latency_ms: 28 + Math.round(Math.random() * 24) };
    this.emit({ type: "monitor", data: s.security.monitor });
    this.emit({ type: "ping", data: null });
  }

  setFeed(scenario: FeedScenario, symbol?: string) {
    const s = this.s;
    const sym = symbol && s.prices[symbol] ? symbol : scenario === "dip_to_trigger" ? "TATAMOTORS" : "RELIANCE";
    s.feedMode = scenario;
    s.feedSymbol = sym;
    s.staleSince = scenario === "stale" ? Date.now() : null;
    for (const [k, p] of Object.entries(s.prices)) {
      const base = BASE[k].ltp;
      if (scenario === "calm") p.target = base;
      else if (scenario === "crash") p.target = Math.round(base * 0.94);
      else if (k === sym && scenario === "spike") p.target = Math.round(base * 1.04);
      else if (k === sym && scenario === "dip_to_trigger") {
        const mandate = [...s.proposals.values()].find((pp) => pp.mandate?.instrument.symbol === sym && pp.mandate.state === "ACTIVE");
        const threshold = mandate?.mandate?.predicate.threshold;
        p.target = threshold && mandate?.mandate?.predicate.op === "LTE" ? Math.round(threshold * 0.985) : Math.round(base * 0.93);
      } else p.target = p.target ?? base;
    }
    if (scenario === "stale") {
      for (const sym2 of WATCHLIST) this.emit({ type: "tick", data: this.tickFor(sym2, true) });
      this.securityEvent("warning", "FEED_STALE", "Market feed stopped. Ticks older than 5 s are marked stale; approvals and mandates pause.", { scenario });
    } else {
      s.security.feed = { ...s.security.feed, healthy: true };
    }
    this.audit("user:demo", "DEMO_FEED_SCENARIO", "feed", scenario);
  }

  // ---- mandates
  private evaluateMandates(symbol: string) {
    for (const p of this.s.proposals.values()) {
      const m = p.mandate;
      if (!m || m.state !== "ACTIVE" || m.instrument.symbol !== symbol) continue;
      const ltp = this.s.prices[symbol].ltp;
      const hit = m.predicate.op === "LTE" ? ltp <= m.predicate.threshold : ltp >= m.predicate.threshold;
      if (hit) void this.fireMandate(p.id, ltp);
    }
  }

  private async fireMandate(proposalId: string, ltp: number) {
    const p = this.s.proposals.get(proposalId);
    if (!p?.mandate) return;
    const eventId = `tick:${p.mandate.instrument.symbol}:${this.s.seq}`;
    this.updateMandate(proposalId, { state: "TRIGGERED", triggered_at: new Date().toISOString(), trigger_event_id: eventId });
    this.outcome("MANDATE_TRIGGERED", "warning", `Mandate triggered: ${p.mandate.instrument.symbol}`, `${p.mandate.condition_text} met at LTP ${rs(ltp)} (event ${eventId}).`, { proposal_id: proposalId });
    this.audit("monitor", "MANDATE_TRIGGERED", "mandate", p.mandate.mandate_id);
    if (p.mandate.kind === "ALERT") {
      await sleep(400);
      this.updateMandate(proposalId, { state: "FIRED", fired_at: new Date().toISOString() });
      this.setStatus(proposalId, "COMPLETED");
      this.outcome("ALERT", "warning", `Alert fired: ${p.mandate.instrument.symbol}`, `LTP ${rs(ltp)}. ${p.mandate.condition_text}.`, { proposal_id: proposalId });
      return;
    }
    await sleep(600);
    this.updateMandate(proposalId, { state: "EXECUTING" });
    this.setStatus(proposalId, "EXECUTING");
    await this.execute(proposalId, { fromMandate: true });
    const after = this.s.proposals.get(proposalId);
    const filled = after?.steps[0]?.state === "FILLED";
    this.updateMandate(proposalId, { state: filled ? "FIRED" : "FAILED", fired_at: filled ? new Date().toISOString() : null });
  }

  updateMandate(proposalId: string, patch: Partial<MandateView>) {
    const p = this.s.proposals.get(proposalId);
    if (!p?.mandate) return;
    const mandate = { ...p.mandate, ...patch };
    this.s.proposals.set(proposalId, { ...p, mandate, updated_at: new Date().toISOString() });
    this.emit({ type: "mandate", data: mandate });
  }

  // ---- proposals
  setStatus(id: string, status: ProposalStatus, patch: Partial<Proposal> = {}) {
    const p = this.s.proposals.get(id);
    if (!p) return;
    this.s.proposals.set(id, { ...p, ...patch, status, updated_at: new Date().toISOString() });
    this.emit({ type: "proposal", data: { id, status } });
  }

  patchProposal(id: string, fn: (p: Proposal) => Proposal) {
    const p = this.s.proposals.get(id);
    if (!p) return;
    this.s.proposals.set(id, { ...fn(p), updated_at: new Date().toISOString() });
  }

  addProposal(p: Proposal) {
    this.s.proposals.set(p.id, p);
    this.audit("api", "PROPOSAL_CREATED", "proposal", p.id);
    this.audit("verifier", p.status === "BLOCKED" ? "VERIFIER_BLOCKED" : "VERIFIER_PASSED", "proposal", p.id);
  }

  specFor(partial: Omit<OrderSpec, "ltp" | "heldQty" | "availableMargin" | "exposure">) {
    return specFor(this.s, partial);
  }

  summary() {
    return summaryOf(this.s);
  }

  // ---- approvals
  approvalStart(id: string, method: PendingApproval["method"]) {
    const p = this.s.proposals.get(id);
    if (!p) throw new ApiError(404, "NOT_FOUND", `Proposal ${id} does not exist.`);
    if (this.s.security.kill_switch.engaged) {
      throw new ApiError(423, "KILL_SWITCH_ENGAGED", "The kill switch is engaged. All approvals and executions are halted until it is disengaged with your PIN.");
    }
    if (p.status === "BLOCKED") {
      throw new ApiError(409, "PROPOSAL_BLOCKED", "This proposal is blocked by the verifier and cannot be countersigned. Revise your instruction.");
    }
    if (p.status !== "AWAITING_APPROVAL") {
      throw new ApiError(409, "ALREADY_APPROVED", `This proposal is ${p.status.toLowerCase().replace(/_/g, " ")}; it cannot be countersigned again.`);
    }
    if (method === "webauthn" && !this.s.devices.some((d) => !d.revoked)) {
      throw new ApiError(422, "NO_DEVICE_REGISTERED", "No passkey is registered for this account. Register this device's passkey, or use your app PIN.");
    }
    if (method === "pin" && !this.s.hasPin) {
      throw new ApiError(422, "PIN_NOT_SET", "You have not set an app PIN yet. Set one in the Security Center, or use your passkey.");
    }
    if (method === "pin" && this.s.pinLockedUntil && this.s.pinLockedUntil > Date.now()) {
      throw new ApiError(423, "PIN_LOCKED", `PIN is locked after too many attempts. Try again ${Math.ceil((this.s.pinLockedUntil - Date.now()) / 60000)} min from now, or use your passkey.`);
    }
    if (method === "totp" && !this.s.hasTotp) {
      throw new ApiError(422, "TOTP_NOT_SET", "No authenticator app is set up. Set one up in the Security Center, or use your passkey or PIN.");
    }
    const issued = Date.now();
    const expires = issued + 60_000;
    const preview = p.steps[0]?.preview;
    const payload = {
      account_id: ACCOUNT_ID,
      approval_nonce: fakeHash(`${id}:${issued}`).slice(0, 32),
      band: preview ? { high_paise: preview.band_high_paise, low_paise: preview.band_low_paise } : null,
      canonical_hash: p.canonical_hash,
      expires_at: new Date(expires).toISOString(),
      issued_at: new Date(issued).toISOString(),
      kind: p.kind,
      proposal_id: p.id,
      reference_price_paise: preview?.reference_price_paise ?? null,
      steps: p.steps.length,
    };
    const payload_hash = fakeHash(JSON.stringify(payload));
    const approval_id = `apv_${fakeHash(payload_hash).slice(0, 12)}`;
    this.s.approvals.set(approval_id, { approval_id, proposal_id: id, method, issued_at: issued, expires_at: expires, payload_hash, used: false });
    this.audit("api", "APPROVAL_CHALLENGED", "approval", approval_id);
    const challenge = hexToB64url(payload_hash);
    return {
      approval_id,
      issued_at: payload.issued_at,
      expires_at: payload.expires_at,
      payload,
      payload_hash,
      webauthn:
        method === "webauthn"
          ? {
              challenge,
              timeout: 60_000,
              rpId: typeof location !== "undefined" ? location.hostname : "localhost",
              userVerification: "required" as const,
              allowCredentials: this.s.devices
                .filter((d) => !d.revoked)
                .map((d) => ({ id: hexToB64url(fakeHash(d.id).slice(0, 32)), type: "public-key" as const, transports: ["internal" as const] })),
            }
          : null,
    };
  }

  approvalComplete(id: string, body: { approval_id: string; pin?: string; totp?: string; webauthn?: unknown }): Proposal {
    const a = this.s.approvals.get(body.approval_id);
    const p = this.s.proposals.get(id);
    if (!a || !p || a.proposal_id !== id) {
      throw new ApiError(409, "PAYLOAD_MISMATCH", "This approval does not belong to this proposal. Start a new approval.");
    }
    if (a.used) throw new ApiError(409, "ALREADY_APPROVED", "This approval was already used. Signatures are single-use.");
    if (this.s.security.kill_switch.engaged) {
      throw new ApiError(423, "KILL_SWITCH_ENGAGED", "The kill switch was engaged while you were signing. Nothing was executed.");
    }
    if (Date.now() > a.expires_at) {
      throw new ApiError(409, "APPROVAL_EXPIRED", "The 60 s approval window has closed. Start a new window to countersign; nothing was executed.");
    }
    if (a.method === "pin") {
      if (this.s.pinLockedUntil && this.s.pinLockedUntil > Date.now()) {
        throw new ApiError(423, "PIN_LOCKED", "PIN is locked after too many incorrect attempts. Use your passkey or authenticator code.");
      }
      if (body.pin !== this.s.pin) {
        this.s.pinFails += 1;
        this.securityEvent("info", "PIN_FAILED", `Incorrect app PIN entered (${this.s.pinFails} of 5 attempts).`, { attempts: this.s.pinFails });
        if (this.s.pinFails >= 5) {
          this.s.pinLockedUntil = Date.now() + 15 * MIN;
          throw new ApiError(423, "PIN_LOCKED", "Too many incorrect PIN attempts. PIN is locked for 15 minutes; use your passkey instead.");
        }
        throw new ApiError(422, "PIN_INVALID", `That PIN is incorrect. ${5 - this.s.pinFails} attempts left before the PIN locks.`);
      }
      this.s.pinFails = 0;
    }
    if (a.method === "totp" && body.totp !== MOCK_TOTP) {
      throw new ApiError(422, "TOTP_INVALID", "That authenticator code is not valid for the current 30 s window.");
    }
    if (a.method === "webauthn" && !body.webauthn) {
      throw new ApiError(422, "SIGNATURE_INVALID", "No passkey assertion was provided for this approval.");
    }
    if (this.s.feedMode === "stale") {
      throw new ApiError(409, "STALE_MARKET_DATA", "The last market tick is older than 5 s, so the price band cannot be checked. Wait for the feed to recover.");
    }
    const step = p.steps[0];
    if (step?.preview && p.kind !== "MANDATE") {
      const ltp = this.s.prices[step.canonical.instrument.symbol]?.ltp;
      if (ltp && (ltp < step.preview.band_low_paise || ltp > step.preview.band_high_paise)) {
        throw new ApiError(
          409,
          "PRICE_DRIFT",
          `LTP ${rs(ltp)} is outside your approved band ${rs(step.preview.band_low_paise)} to ${rs(step.preview.band_high_paise)}. This approval is void; review again.`,
        );
      }
    }
    a.used = true;
    const verifiedAt = Date.now();
    const device = this.s.devices.find((d) => !d.revoked);
    const approval = {
      approval_id: a.approval_id,
      status: "VERIFIED" as const,
      method: a.method,
      assurance: a.method === "webauthn" ? ("device_signed" as const) : ("server_attested" as const),
      issued_at: new Date(a.issued_at).toISOString(),
      expires_at: new Date(a.expires_at).toISOString(),
      verified_at: new Date(verifiedAt).toISOString(),
      credential_label: a.method === "webauthn" ? (device?.label ?? "Passkey") : a.method === "pin" ? "App PIN" : "Authenticator app",
      payload_hash: a.payload_hash,
      failure_reason: null,
    };
    if (device && a.method === "webauthn") device.last_used_at = approval.verified_at;
    this.audit("user:demo", "APPROVAL_VERIFIED", "approval", a.approval_id);
    this.outcome("APPROVAL_VERIFIED", "success", "Countersignature verified", `${a.method === "webauthn" ? "Passkey signature" : a.method === "pin" ? "PIN" : "Authenticator code"} verified for ${p.kind.toLowerCase()} ${p.short_hash}. Single-use capability minted, expires in 60 s.`, { proposal_id: id });

    if (p.kind === "MANDATE") {
      this.setStatus(id, "ACTIVE", { approval, can_approve: false });
      this.updateMandate(id, { state: "ACTIVE", activated_at: approval.verified_at });
      this.audit("api", "MANDATE_ARMED", "mandate", p.mandate?.mandate_id ?? id);
      this.outcome("MANDATE_ARMED", "info", `Mandate armed: ${p.mandate?.instrument.symbol}`, `${p.summary}. One-shot.`, { proposal_id: id });
      return this.s.proposals.get(id) as Proposal;
    }
    this.setStatus(id, "APPROVED", { approval, can_approve: false });
    this.audit("api", "CAPABILITY_MINTED", "capability", `cap_${a.approval_id.slice(4, 12)}`);
    void this.execute(id, {});
    return this.s.proposals.get(id) as Proposal;
  }

  reject(id: string): Proposal {
    const p = this.s.proposals.get(id);
    if (!p) throw new ApiError(404, "NOT_FOUND", `Proposal ${id} does not exist.`);
    if (p.status !== "AWAITING_APPROVAL" && p.status !== "BLOCKED") {
      throw new ApiError(409, "INVALID_STATE", `A ${p.status.toLowerCase().replace(/_/g, " ")} proposal cannot be rejected.`);
    }
    this.setStatus(id, "REJECTED_BY_USER", { can_approve: false });
    this.audit("user:demo", "PROPOSAL_REJECTED", "proposal", id);
    return this.s.proposals.get(id) as Proposal;
  }

  // ---- executor simulation
  private consumeFault(): BrokerFault {
    const f = this.s.brokerFault;
    if (f === "none") return f;
    this.s.faultCount -= 1;
    if (this.s.faultCount <= 0) this.s.brokerFault = "none";
    return f;
  }

  private setStep(id: string, stepNo: number, patch: Partial<ProposalStep>, outbox?: Partial<ExecutionState["outbox"][number]>) {
    this.patchProposal(id, (p) => {
      const steps = p.steps.map((st) => (st.step_no === stepNo ? { ...st, ...patch } : st));
      let execution = p.execution;
      if (execution && outbox) {
        const exists = execution.outbox.some((o) => o.step_no === stepNo);
        const rows = exists
          ? execution.outbox.map((o) => (o.step_no === stepNo ? { ...o, ...outbox } : o))
          : [
              ...execution.outbox,
              { client_order_id: "", step_no: stepNo, state: "PENDING", broker_order_id: null, attempts: 0, reconciled: false, ...outbox },
            ];
        execution = { ...execution, outbox: rows };
      }
      return { ...p, steps, execution };
    });
    const p = this.s.proposals.get(id);
    if (p) this.emit({ type: "proposal", data: { id, status: p.status } });
  }

  async execute(id: string, opts: { fromMandate?: boolean }) {
    const p0 = this.s.proposals.get(id);
    if (!p0) return;
    await sleep(650);
    if (this.s.security.kill_switch.engaged) {
      this.setStatus(id, "FAILED", { execution: { job_id: `job_${p0.short_hash}`, status: "HALTED", reason: "Kill switch engaged before execution. Capability was not consumed.", outbox: [] } });
      this.outcome("EXECUTION_BLOCKED", "danger", "Execution halted: kill switch", `Proposal ${p0.short_hash} was not sent to the broker.`, { proposal_id: id });
      return;
    }
    this.setStatus(id, "EXECUTING", { execution: { job_id: `job_${p0.short_hash}`, status: "RUNNING", reason: null, outbox: [] } });
    this.audit("executor", "CAPABILITY_CONSUMED", "capability", `cap_${p0.short_hash}`);
    const results: StepState[] = [];
    for (const st of p0.steps) {
      if (st.state === "FILLED") {
        results.push("FILLED");
        continue;
      }
      if (st.depends_on && results[st.depends_on - 1] !== "FILLED") {
        this.setStep(id, st.step_no, { state: "SKIPPED", reason: `Step ${st.depends_on} did not fill. Stop on failure: no automatic compensating trades.` });
        results.push("SKIPPED");
        continue;
      }
      const tag = `CS-${p0.short_hash}-${st.step_no}`;
      this.setStep(id, st.step_no, { state: "SUBMITTED", client_order_id: tag }, { client_order_id: tag, state: "SUBMITTED", attempts: 1 });
      this.audit("executor", "ORDER_SUBMITTED", "order", tag);
      await sleep(700);
      const fault = this.consumeFault();
      const label = `${st.canonical.side} ${st.canonical.quantity} ${st.canonical.instrument.symbol}`;
      if (fault === "connection_failure") {
        this.setStep(id, st.step_no, { state: "FAILED", reason: "Broker connection failed. Reconciled by tag: no order exists at the broker, so nothing to retry." }, { state: "FAILED", reconciled: true });
        this.outcome("ORDER_REJECTED", "danger", `Broker unreachable: ${label}`, `Connection failed for ${tag}. Reconciled: not at broker. Not retried automatically.`, { proposal_id: id });
        results.push("FAILED");
        continue;
      }
      if (fault === "reject") {
        const brokerId = `PB-${fakeHash(tag).slice(0, 6).toUpperCase()}`;
        this.setStep(id, st.step_no, { state: "REJECTED", broker_order_id: brokerId, reason: "RMS: Order price outside daily circuit band (simulated broker fault)." }, { state: "REJECTED", broker_order_id: brokerId });
        this.pushOrder(st, brokerId, tag, "REJECTED", 0, "RMS: Order price outside daily circuit band (simulated broker fault).");
        this.outcome("ORDER_REJECTED", "danger", `Rejected by broker: ${label}`, "RMS rejected the order. Not retried; no compensating trade.", { proposal_id: id, broker_order_id: brokerId });
        results.push("REJECTED");
        continue;
      }
      const brokerId = `PB-${fakeHash(tag).slice(0, 6).toUpperCase()}`;
      if (fault === "accept_then_drop") {
        await sleep(1100);
        this.setStep(id, st.step_no, { state: "OPEN", broker_order_id: brokerId }, { state: "OPEN", broker_order_id: brokerId, reconciled: true });
        this.outcome("RECONCILED", "info", `Reconciled, not retried: ${label}`, `Broker ack was lost. Executor found ${brokerId} by tag ${tag} and did not resend.`, { proposal_id: id, broker_order_id: brokerId });
        this.audit("executor", "RECONCILED_BY_TAG", "order", tag);
      } else {
        if (fault === "delay_ack") await sleep(1800);
        this.setStep(id, st.step_no, { state: "OPEN", broker_order_id: brokerId }, { state: "OPEN", broker_order_id: brokerId });
        this.outcome("ORDER_ACCEPTED", "info", `Order accepted: ${label}`, `Broker acknowledged ${brokerId} for client tag ${tag}.`, { proposal_id: id, broker_order_id: brokerId });
      }
      this.audit("broker", "ORDER_ACKNOWLEDGED", "order", brokerId);
      await sleep(900);
      const fillPrice = st.canonical.limit_price_paise ?? this.s.prices[st.canonical.instrument.symbol]?.ltp ?? st.preview?.reference_price_paise ?? 0;
      if (fault === "partial_fill") {
        const filled = Math.max(1, Math.floor(st.canonical.quantity / 2));
        this.setStep(id, st.step_no, { state: "PARTIALLY_FILLED", filled_quantity: filled, avg_fill_price_paise: fillPrice, reason: `Broker filled ${filled} of ${st.canonical.quantity}; remainder open.` }, { state: "PARTIALLY_FILLED" });
        this.pushOrder(st, brokerId, tag, "PARTIALLY_FILLED", filled, null, fillPrice);
        this.applyFill(st, filled, fillPrice);
        this.outcome("ORDER_PARTIALLY_FILLED", "warning", `Partially filled: ${label}`, `${filled} of ${st.canonical.quantity} filled @ ${rs(fillPrice)}. The rest stays open; nothing is retried.`, { proposal_id: id, broker_order_id: brokerId });
        results.push("PARTIALLY_FILLED");
        continue;
      }
      this.setStep(id, st.step_no, { state: "FILLED", filled_quantity: st.canonical.quantity, avg_fill_price_paise: fillPrice }, { state: "FILLED" });
      this.pushOrder(st, brokerId, tag, "COMPLETE", st.canonical.quantity, null, fillPrice);
      this.applyFill(st, st.canonical.quantity, fillPrice);
      this.audit("broker", "ORDER_FILLED", "order", brokerId);
      this.outcome("ORDER_FILLED", "success", `Filled: ${label}`, `${st.canonical.quantity} @ ${rs(fillPrice)} avg.${opts.fromMandate ? " Fired by your standing mandate." : ""}`, { proposal_id: id, broker_order_id: brokerId });
      results.push("FILLED");
    }
    const allFilled = results.every((r) => r === "FILLED");
    const anyFilled = results.some((r) => r === "FILLED" || r === "PARTIALLY_FILLED");
    const status: ProposalStatus = allFilled ? "COMPLETED" : anyFilled ? "PARTIALLY_COMPLETED" : "FAILED";
    const p1 = this.s.proposals.get(id);
    this.setStatus(id, opts.fromMandate ? "COMPLETED" : status, {
      execution: p1?.execution ? { ...p1.execution, status: allFilled ? "DONE" : anyFilled ? "DONE" : "REJECTED", reason: allFilled ? null : "Stopped on failure. No automatic compensating trades." } : null,
    });
    if (p0.kind === "PLAN") {
      const kind = allFilled ? "PLAN_COMPLETED" : anyFilled ? "PLAN_PARTIALLY_COMPLETED" : "PLAN_FAILED";
      this.outcome(
        kind,
        allFilled ? "success" : anyFilled ? "warning" : "danger",
        allFilled ? `Plan completed: ${results.length} of ${results.length} steps filled` : `Plan stopped: ${results.filter((r) => r === "FILLED").length} of ${results.length} steps filled`,
        allFilled ? "One signature bound every step." : "Stop on failure: remaining steps skipped, no automatic compensating trades.",
        { proposal_id: id },
      );
    }
  }

  private pushOrder(st: ProposalStep, brokerId: string, tag: string, status: BrokerOrder["status"], filled: number, reason: string | null, avg?: number) {
    const now = new Date().toISOString();
    const c = st.canonical;
    this.s.orders = [
      {
        broker_order_id: brokerId,
        client_order_id: tag,
        instrument: c.instrument,
        side: c.side,
        quantity: c.quantity,
        filled_quantity: filled,
        pending_quantity: status === "COMPLETE" || status === "REJECTED" ? 0 : c.quantity - filled,
        order_type: c.order_type,
        product: c.product,
        validity: c.validity,
        limit_price_paise: c.limit_price_paise,
        trigger_price_paise: c.trigger_price_paise,
        protection_price_paise: c.protection_price_paise,
        avg_fill_price_paise: avg ?? null,
        status,
        reject_reason: reason,
        placed_at: now,
        updated_at: now,
      },
      ...this.s.orders.filter((o) => o.broker_order_id !== brokerId),
    ];
  }

  private applyFill(st: ProposalStep, qty: number, price: number) {
    const sym = st.canonical.instrument.symbol;
    const h = this.s.holdings.find((x) => x.symbol === sym);
    if (st.canonical.side === "BUY") {
      if (h) {
        h.avg = Math.round((h.avg * h.qty + price * qty) / (h.qty + qty));
        h.qty += qty;
      } else if (INSTRUMENTS[sym]) {
        this.s.holdings.push({ symbol: sym, qty, avg: price });
      }
      this.s.cash -= price * qty;
    } else if (h) {
      h.qty -= qty;
      this.s.cash += price * qty;
      if (h.qty <= 0) this.s.holdings = this.s.holdings.filter((x) => x !== h);
    }
  }

  // ---- outcomes, audit, security
  outcome(kind: OutcomeEvent["kind"], severity: OutcomeEvent["severity"], title: string, detail: string, extra: Partial<OutcomeEvent> = {}) {
    const o: OutcomeEvent = {
      id: `out_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`,
      ts: new Date().toISOString(),
      kind,
      severity,
      title,
      detail,
      proposal_id: null,
      broker_order_id: null,
      ...extra,
    };
    this.s.outcomes = [o, ...this.s.outcomes].slice(0, 300);
    this.emit({ type: "outcome", data: o });
  }

  audit(actor: string, event_type: string, subject_type: string, subject_id: string) {
    const prev = this.s.audit.at(-1);
    const id = (prev?.id ?? 0) + 1;
    const ts = new Date().toISOString();
    const prev_hash = prev?.hash ?? "0".repeat(64);
    const hash = fakeHash(`${prev_hash}|${id}|${ts}|${actor}|${event_type}|${subject_id}`);
    this.s.audit.push({ id, ts, actor, event_type, subject_type, subject_id, data: {}, prev_hash, hash });
    this.emit({ type: "audit", data: { id, event_type, ts } });
  }

  securityEvent(severity: SecurityEvent["severity"], kind: string, message: string, data: Record<string, unknown> = {}) {
    const e: SecurityEvent = { id: `sec_${Date.now().toString(36)}`, ts: new Date().toISOString(), severity, kind, message, data, resolved: severity === "info" };
    this.s.securityEvents = [e, ...this.s.securityEvents];
    this.emit({ type: "security", data: e });
  }

  killSwitch(engaged: boolean, pin?: string): SecurityStatus {
    const ks = this.s.security.kill_switch;
    if (engaged) {
      this.s.security.kill_switch = { engaged: true, reason: "Engaged manually from the Security Center", at: new Date().toISOString() };
      this.outcome("KILL_SWITCH", "danger", "Kill switch engaged", "All executions and approvals are halted. Disengaging requires your PIN.");
      this.securityEvent("critical", "KILL_SWITCH_ENGAGED", "Kill switch engaged by Demo Trader. Executor stopped consuming capabilities.");
      this.audit("user:demo", "KILL_SWITCH_ENGAGED", "system", "kill_switch");
    } else {
      if (!ks.engaged) return this.s.security;
      if (pin !== this.s.pin) {
        throw new ApiError(422, "PIN_INVALID", "That PIN is incorrect. The kill switch stays engaged.");
      }
      this.s.security.kill_switch = { engaged: false, reason: null, at: new Date().toISOString() };
      this.outcome("KILL_SWITCH", "info", "Kill switch disengaged", "Executions resume. Pending approvals still need fresh countersignatures.");
      this.securityEvent("warning", "KILL_SWITCH_DISENGAGED", "Kill switch disengaged with PIN.");
      this.audit("user:demo", "KILL_SWITCH_DISENGAGED", "system", "kill_switch");
    }
    return this.s.security;
  }

  runAttack(scenarioId: string): AttackRun {
    const ids = scenarioId === "all" ? SCENARIOS.map((s) => s.id) : [scenarioId];
    if (!ids.every((i) => SCENARIOS.some((s) => s.id === i))) {
      throw new ApiError(404, "SCENARIO_NOT_FOUND", `Attack scenario "${scenarioId}" does not exist.`);
    }
    const results = SCENARIOS.filter((sc) => ids.includes(sc.id)).map(scenarioResult);
    const sb = this.s.scoreboard;
    const attempted = results.reduce((a, r) => a + r.attempted_actions, 0);
    const blocked = results.reduce((a, r) => a + r.blocked_actions, 0);
    this.s.scoreboard = {
      unapproved_executions: sb.unapproved_executions + results.reduce((a, r) => a + r.unapproved_orders, 0),
      naive_unapproved_executions: sb.naive_unapproved_executions + results.reduce((a, r) => a + r.naive_unapproved_orders, 0),
      attacks_attempted: sb.attacks_attempted + attempted,
      attacks_blocked: sb.attacks_blocked + blocked,
      last_run_at: new Date().toISOString(),
    };
    const run = makeRun(ids, Date.now(), this.s.scoreboard);
    this.s.runs = [run, ...this.s.runs].slice(0, 10);
    for (const r of results) {
      this.outcome("EXECUTION_BLOCKED", "danger", `Attack ${r.letter} blocked: ${r.title}`, r.verdict);
    }
    this.audit("attack-lab", "ATTACK_RUN_COMPLETED", "attack_run", run.id);
    return run;
  }
}

function rs(paise: number): string {
  const r = (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `₹${r}`;
}

function hexToB64url(hex: string): string {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export const engine = new MockEngine();
export { rs, isoAgo, isoIn };
