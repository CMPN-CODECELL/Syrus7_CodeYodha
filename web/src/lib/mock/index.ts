// Mock implementation of CountersignApi (NEXT_PUBLIC_MOCK=1). Same interface as the real client.

import type { CountersignApi } from "../api";
import { EMAIL_RE, PASSWORD_MAX, PASSWORD_MIN, weakPinReason } from "../auth";
import { ApiError } from "../errors";
import type { HistoryPoint, Me, OptionChain, PublicStatus, Quote } from "../types";
import { chatHistory, simulateChat } from "./chat";
import { MOCK_TOTP, engine, sleep } from "./engine";
import {
  EXPIRIES,
  INSTRUMENTS,
  OPTION_POSITION,
  SCENARIOS,
  WATCHLIST,
  buildHolding,
  buildOptionChain,
  buildPosition,
  mulberry32,
  seedMe,
  strSeed,
} from "./fixtures";

import { findUser, findUserByEmail, newAccountId, newUserId, passkeyOwner, setPasskeyOwner, upsertUser } from "./users";

export { MOCK_TOTP } from "./engine";

const latency = (min = 90, max = 260) => sleep(min + Math.random() * (max - min));
const clone = <T,>(v: T): T => structuredClone(v);

async function authed<T>(fn: () => T | Promise<T>, ms?: [number, number]): Promise<T> {
  await latency(...(ms ?? [90, 260]));
  engine.requireAuth();
  return clone(await fn());
}

function me(): Me {
  const s = engine.s;
  const u = findUser(s.userId);
  const m = seedMe(s.devices, s.hasPin, s.hasTotp);
  m.pin_locked_until = s.pinLockedUntil && s.pinLockedUntil > Date.now() ? new Date(s.pinLockedUntil).toISOString() : null;
  const ses = s.sessions.find((x) => x.current);
  if (ses) m.session = { id: ses.id, created_at: ses.created_at, expires_at: ses.expires_at };
  if (u) {
    m.user = { id: u.id, username: u.email, display_name: u.display_name };
    m.account_id = u.account_id;
  }
  const passkeys = s.devices.filter((d) => !d.revoked && !d.is_virtual).length;
  return {
    ...m,
    email: u?.email ?? null,
    account: { id: m.account_id, label: "Paper trading account", kind: "paper" },
    onboarding: { pin_set: s.hasPin, passkeys, totp: s.hasTotp, complete: s.hasPin },
    demo_mode: true,
  };
}

// Failed sign-ins per email: 5 free attempts, then 30 s doubling up to 15 min (mirrors the API).
const loginFails = new Map<string, { count: number; until: number }>();

function checkLoginLock(email: string) {
  const f = loginFails.get(email);
  if (f && f.until > Date.now()) {
    const retry = Math.ceil((f.until - Date.now()) / 1000);
    throw new ApiError(429, "LOGIN_LOCKED", `Too many failed sign-in attempts. Try again in ${retry} s.`, undefined, { retry_after_s: retry });
  }
}

function recordLoginFail(email: string) {
  const f = loginFails.get(email) ?? { count: 0, until: 0 };
  f.count += 1;
  if (f.count >= 5) f.until = Date.now() + Math.min(900, 30 * 2 ** (f.count - 5)) * 1000;
  loginFails.set(email, f);
}

function invalid(field: string, message: string): never {
  throw new ApiError(422, "INVALID_INPUT", message, undefined, { field });
}

function assertStrongPin(pin: string) {
  if (!/^\d{4}$/.test(pin)) throw new ApiError(422, "PIN_FORMAT", "The PIN must be exactly 4 digits.");
  const weak = weakPinReason(pin);
  if (weak) throw new ApiError(422, "WEAK_PIN", `That PIN is too easy to guess. ${weak}`);
}

function publicStatus(): PublicStatus {
  const s = engine.s;
  const n = s.prices.NIFTY;
  return {
    all_operational: s.feedMode !== "stale" && !s.security.kill_switch.engaged,
    systems: { api: true, broker: true, feed: s.feedMode !== "stale", executor: true, monitor: true },
    unapproved_executions: s.scoreboard.unapproved_executions,
    attacks_attempted: s.scoreboard.attacks_attempted,
    attacks_blocked: s.scoreboard.attacks_blocked,
    last_attack_run_at: s.scoreboard.last_run_at,
    audit_events: s.audit.length,
    market: { mode: "replay", nifty: n ? { ltp_paise: n.ltp, change_bps: Math.round(((n.ltp - n.prev) / n.prev) * 10_000) } : null },
    generated_at: new Date().toISOString(),
  };
}

function quoteFor(symbol: string): Quote {
  const sym = symbol.toUpperCase();
  if (sym === OPTION_POSITION.symbol) return { instrument: OPTION_POSITION, tick: engine.optionTick() };
  if (!INSTRUMENTS[sym]) throw new ApiError(404, "INSTRUMENT_NOT_FOUND", `No instrument named "${symbol}" on NSE.`);
  return { instrument: INSTRUMENTS[sym], tick: engine.tickFor(sym, engine.s.feedMode === "stale") };
}

function history(symbol: string, points: number): HistoryPoint[] {
  const sym = symbol.toUpperCase();
  const p = engine.s.prices[sym];
  if (!p) throw new ApiError(404, "INSTRUMENT_NOT_FOUND", `No price history for "${symbol}".`);
  const rand = mulberry32(strSeed(sym));
  const now = Math.floor(Date.now() / 1000);
  const step = 60;
  const out: HistoryPoint[] = [];
  let v = p.ltp / 100;
  const vol = sym === "NIFTY" ? 0.0007 : 0.0016;
  for (let i = 0; i < points; i++) {
    out.push({ time: now - i * step, value: Math.round(v * 100) / 100 });
    const drift = (p.prev / 100 - v) * 0.004;
    v = Math.max(1, v - drift + (rand() - 0.5) * v * vol * 2);
  }
  return out.reverse();
}

export const mockApi: CountersignApi = {
  async publicStatus() {
    await latency(40, 120);
    engine.ensureLoginRestored();
    return publicStatus();
  },
  async signup({ display_name, email, password }) {
    await latency(300, 520);
    const name = (display_name ?? "").trim();
    const mail = (email ?? "").trim();
    if (!name || name.length > 64) invalid("display_name", "Display name must be 1 to 64 characters.");
    if (!EMAIL_RE.test(mail)) invalid("email", "Enter a valid email address.");
    if (!password || password.length < PASSWORD_MIN || password.length > PASSWORD_MAX) {
      invalid("password", `Password must be ${PASSWORD_MIN} to ${PASSWORD_MAX} characters.`);
    }
    if (password.trim().toLowerCase() === mail.toLowerCase()) invalid("password", "Password must not be the same as your email.");
    if (findUserByEmail(mail)) throw new ApiError(409, "EMAIL_TAKEN", "An account with this email already exists. Sign in instead.");
    const u = {
      id: newUserId(),
      email: mail,
      display_name: name,
      password,
      account_id: newAccountId(),
      created_at: new Date().toISOString(),
      pin: null,
      has_totp: false,
      devices: [],
      pin_fails: 0,
      pin_locked_until: null,
    };
    upsertUser(u);
    engine.signIn(u);
    engine.audit(`user:${u.id}`, "USER_SIGNED_UP", "user", u.id);
    return clone(me());
  },
  async login({ email, password }) {
    await latency(280, 480);
    const key = (email ?? "").trim().toLowerCase();
    checkLoginLock(key);
    const u = findUserByEmail(key);
    if (!u || u.password !== password) {
      recordLoginFail(key);
      throw new ApiError(401, "INVALID_CREDENTIALS", "The email or password is incorrect.");
    }
    loginFails.delete(key);
    engine.signIn(u);
    return clone(me());
  },
  async passkeyLoginStart() {
    await latency(80, 160);
    const challenge = btoa(String(Math.random())).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
    return {
      challenge_id: `chl_${Date.now().toString(36)}`,
      options: {
        challenge,
        rpId: typeof location !== "undefined" ? location.hostname : "localhost",
        allowCredentials: [],
        userVerification: "required" as const,
        timeout: 60_000,
      },
    };
  },
  async passkeyLoginFinish() {
    await latency(200, 380);
    const u = findUser(passkeyOwner());
    if (!u || !u.devices.some((d) => !d.revoked && !d.is_virtual)) {
      throw new ApiError(401, "PASSKEY_NOT_RECOGNIZED", "This passkey is not registered to any Countersign account. Sign in with your email and password.");
    }
    engine.signIn(u);
    return clone(me());
  },
  async me() {
    await latency(60, 140);
    engine.requireAuth();
    return clone(me());
  },
  async logout() {
    await latency();
    engine.setLoggedIn(false);
    return { ok: true };
  },
  registerStart: (device_label) =>
    authed(() => {
      engine.s.pendingDeviceLabel = device_label ?? "This device";
      const challenge = btoa(String(Math.random())).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
      return {
        rp: { name: "Countersign", id: typeof location !== "undefined" ? location.hostname : "localhost" },
        user: { id: "dXNyX2RlbW8", name: "demo", displayName: "Demo Trader" },
        challenge,
        pubKeyCredParams: [
          { type: "public-key" as const, alg: -7 },
          { type: "public-key" as const, alg: -257 },
        ],
        timeout: 60_000,
        attestation: "none" as const,
        authenticatorSelection: { residentKey: "preferred" as const, userVerification: "required" as const },
        excludeCredentials: [],
      };
    }),
  registerFinish: () =>
    authed(() => {
      const d = {
        id: `dev_${Date.now().toString(36)}`,
        label: engine.s.pendingDeviceLabel,
        created_at: new Date().toISOString(),
        last_used_at: null,
        revoked: false,
        is_virtual: false,
        transports: ["internal", "hybrid"],
      };
      engine.s.devices = [...engine.s.devices, d];
      engine.securityEvent("info", "PASSKEY_REGISTERED", `Passkey registered: ${d.label}.`, { device_id: d.id });
      engine.audit("user:demo", "PASSKEY_REGISTERED", "device", d.id);
      engine.syncUser();
      if (engine.s.userId) setPasskeyOwner(engine.s.userId);
      return d;
    }, [300, 500]),
  setPin: (pin, current_pin) =>
    authed(() => {
      if (engine.s.hasPin && current_pin !== engine.s.pin) {
        throw new ApiError(422, "PIN_INVALID", "Your current PIN is incorrect, so the PIN was not changed.");
      }
      assertStrongPin(pin);
      engine.s.pin = pin;
      engine.s.hasPin = true;
      engine.s.pinFails = 0;
      engine.s.pinLockedUntil = null;
      engine.audit("user:demo", "PIN_SET", "user", "usr_demo");
      engine.syncUser();
      return { ok: true as const };
    }),
  pinReset: ({ password, new_pin, totp }) =>
    authed(() => {
      const u = findUser(engine.s.userId);
      if (!u || u.password !== password) throw new ApiError(401, "INVALID_CREDENTIALS", "That password is incorrect, so the PIN was not reset.");
      if (engine.s.hasTotp) {
        if (!totp) throw new ApiError(422, "TOTP_REQUIRED", "Enter the current 6-digit code from your authenticator app.");
        if (totp !== MOCK_TOTP) throw new ApiError(422, "TOTP_INVALID", "That authenticator code does not match. Enter the current 6-digit code.");
      }
      assertStrongPin(new_pin);
      engine.s.pin = new_pin;
      engine.s.hasPin = true;
      engine.s.pinFails = 0;
      engine.s.pinLockedUntil = null;
      engine.securityEvent("warning", "PIN_RESET", "App PIN reset with the account password.", {});
      engine.audit("user:demo", "PIN_RESET", "user", engine.s.userId ?? "");
      engine.syncUser();
      return { ok: true as const };
    }, [300, 500]),
  stepUpStart: () =>
    authed(() => ({
      challenge_id: `ch_${Date.now().toString(36)}`,
      options: {
        challenge: btoa(String(Math.random())).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_"),
        rpId: typeof location !== "undefined" ? location.hostname : "localhost",
        allowCredentials: engine.s.devices.filter((d) => !d.revoked).map((d) => ({ id: d.id, type: "public-key" as const })),
        userVerification: "required" as const,
        timeout: 120_000,
      },
    })),
  stepUp: ({ method, pin, code }) =>
    authed(() => {
      if (method === "pin" && pin !== engine.s.pin) throw new ApiError(401, "PIN_INVALID", "Wrong PIN. Try again.");
      if (method === "totp" && code !== MOCK_TOTP) throw new ApiError(401, "TOTP_INVALID", "That authenticator code is not valid.");
      return { ok: true as const, method, expires_at: new Date(Date.now() + 10 * 60_000).toISOString() };
    }),
  totpSetup: () =>
    authed(() => ({
      otpauth_uri: "otpauth://totp/Countersign:demo?secret=JBSWY3DPEHPK3PXP&issuer=Countersign&algorithm=SHA1&digits=6&period=30",
      secret: "JBSWY3DPEHPK3PXP",
    })),
  totpVerify: (code) =>
    authed(() => {
      if (code !== MOCK_TOTP) throw new ApiError(422, "TOTP_INVALID", "That code does not match. Enter the current 6-digit code from your authenticator app.");
      engine.s.hasTotp = true;
      engine.audit("user:demo", "TOTP_ENABLED", "user", "usr_demo");
      engine.syncUser();
      return { ok: true as const };
    }),
  revokeDevice: (id) =>
    authed(() => {
      const d = engine.s.devices.find((x) => x.id === id);
      if (!d) throw new ApiError(404, "NOT_FOUND", "That device is not registered on this account.");
      engine.s.devices = engine.s.devices.map((x) => (x.id === id ? { ...x, revoked: true } : x));
      engine.securityEvent("warning", "PASSKEY_REVOKED", `Passkey revoked: ${d.label}.`, { device_id: id });
      engine.audit("user:demo", "PASSKEY_REVOKED", "device", id);
      engine.syncUser();
      return { ok: true as const };
    }),
  sessions: () => authed(() => engine.s.sessions),
  revokeSession: (id) =>
    authed(() => {
      const ses = engine.s.sessions.find((x) => x.id === id);
      if (!ses) throw new ApiError(404, "NOT_FOUND", "That session does not exist.");
      if (ses.current) throw new ApiError(409, "CURRENT_SESSION", "Use Sign out to end the session you are using.");
      engine.s.sessions = engine.s.sessions.map((x) => (x.id === id ? { ...x, revoked: true } : x));
      engine.audit("user:demo", "SESSION_REVOKED", "session", id);
      return { ok: true as const };
    }),

  accountSummary: () =>
    authed(() => {
      const sum = engine.summary();
      return { ...sum, account_id: findUser(engine.s.userId)?.account_id ?? sum.account_id };
    }),
  holdings: () => authed(() => engine.s.holdings.map((h) => buildHolding(h, engine.s.prices[h.symbol].ltp, engine.s.prices[h.symbol].prev))),
  positions: () =>
    authed(() =>
      engine.s.positions.map((p) =>
        buildPosition(p, p.inst.instrument_id === OPTION_POSITION.instrument_id ? engine.s.optionLtp : engine.s.prices[p.inst.symbol].ltp),
      ),
    ),
  orders: (status) =>
    authed(() => {
      // Orders placed outside Countersign only exist in the (simulated) 021 order book.
      const all = engine.s.activeBroker === "021" ? engine.s.orders : engine.s.orders.filter((o) => o.origin !== "external");
      if (status === "open") return all.filter((o) => o.status === "OPEN" || o.status === "PARTIALLY_FILLED" || o.status === "TRIGGER_PENDING");
      if (status === "rejected") return all.filter((o) => o.status === "REJECTED");
      return all;
    }),
  brokerAccounts: () => authed(() => engine.brokerAccounts(), [60, 140]),
  setActiveBroker: (account_id) => authed(() => engine.setActiveBroker(account_id), [250, 450]),

  instruments: (q) =>
    authed(() => {
      const needle = q.trim().toUpperCase();
      return Object.values(INSTRUMENTS).filter((i) => !needle || i.symbol.includes(needle) || i.display_name.toUpperCase().includes(needle));
    }),
  quote: (symbol) => authed(() => quoteFor(symbol)),
  watchlist: () => authed(() => WATCHLIST.map((s) => quoteFor(s))),
  history: (symbol, points = 300) => authed(() => history(symbol, points), [120, 300]),
  optionChain: ({ expiry, width = 8 }) =>
    authed((): OptionChain => {
      const e = expiry && EXPIRIES.includes(expiry) ? expiry : EXPIRIES[0];
      return buildOptionChain(engine.s.prices.NIFTY.ltp, e, width);
    }, [150, 320]),

  async chat(req, { signal, onEvent }) {
    engine.requireAuth();
    try {
      await simulateChat(req, onEvent, signal);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      throw e;
    }
  },
  chatHistory: (conversation_id) => authed(() => chatHistory(conversation_id)),

  proposals: (params = {}) =>
    authed(() => {
      let list = [...engine.s.proposals.values()].reverse();
      if (params.kind) list = list.filter((p) => p.kind === params.kind);
      if (params.status) list = list.filter((p) => p.status === params.status);
      return list.slice(0, params.limit ?? 100);
    }),
  proposal: (id) =>
    authed(() => {
      const p = engine.s.proposals.get(id);
      if (!p) throw new ApiError(404, "NOT_FOUND", `Proposal ${id} does not exist.`);
      return p;
    }, [60, 140]),
  rejectProposal: (id) => authed(() => engine.reject(id)),
  approvalStart: (id, method) => authed(() => engine.approvalStart(id, method), [160, 320]),
  approvalComplete: (id, body) =>
    authed(() => {
      try {
        return engine.approvalComplete(id, body);
      } finally {
        // PIN failures and lockouts belong to the user, not to this tab.
        engine.syncUser();
      }
    }, [280, 520]),

  mandates: () =>
    authed(() =>
      [...engine.s.proposals.values()]
        .filter((p) => p.mandate && p.mandate.state !== "PENDING_APPROVAL")
        .reverse()
        .map((p) => ({ ...p.mandate!, proposal_id: p.id })),
    ),
  cancelMandate: (id) =>
    authed(() => {
      const p = [...engine.s.proposals.values()].find((x) => x.mandate?.mandate_id === id);
      if (!p?.mandate) throw new ApiError(404, "NOT_FOUND", `Mandate ${id} does not exist.`);
      if (!["ACTIVE", "NEEDS_REVIEW", "PENDING_APPROVAL"].includes(p.mandate.state)) {
        throw new ApiError(409, "INVALID_STATE", `A ${p.mandate.state.toLowerCase().replace(/_/g, " ")} mandate cannot be cancelled.`);
      }
      engine.updateMandate(p.id, { state: "CANCELLED" });
      engine.setStatus(p.id, "CANCELLED");
      engine.audit("user:demo", "MANDATE_CANCELLED", "mandate", id);
      return { ...engine.s.proposals.get(p.id)!.mandate!, proposal_id: p.id };
    }),
  plans: () => authed(() => [...engine.s.proposals.values()].filter((p) => p.kind === "PLAN").reverse()),
  outcomes: (limit = 50) => authed(() => engine.s.outcomes.slice(0, limit)),

  audit: (params = {}) =>
    authed(() => {
      const list = [...engine.s.audit].reverse();
      const filtered = params.before_id ? list.filter((e) => e.id < (params.before_id as number)) : list;
      return filtered.slice(0, params.limit ?? 100);
    }),
  auditVerify: () =>
    authed(() => {
      const head = engine.s.audit.at(-1);
      return {
        ok: true,
        checked: engine.s.audit.length,
        head_hash: head?.hash ?? "",
        broken_at_id: null,
        detail: `All ${engine.s.audit.length} events link to their predecessor by SHA-256 prev_hash.`,
      };
    }, [500, 900]),
  securityStatus: () =>
    authed(() => {
      const on021 = engine.s.activeBroker === "021";
      const sec = engine.s.security;
      return {
        ...sec,
        broker: { ...sec.broker, kind: engine.s.activeBroker, label: on021 ? "021 Sandbox" : "Paper broker" },
        feed: { ...sec.feed, mode: on021 ? "live" : "replay" },
      };
    }, [50, 120]),
  securityEvents: (scope = "mine") =>
    authed(() => engine.s.securityEvents.filter((e) => scope === "all" || e.scope !== "drill")),
  killSwitch: (engaged, pin) => authed(() => engine.killSwitch(engaged, pin), [200, 380]),

  scenarios: () => authed(() => SCENARIOS),
  runAttack: (scenario_id) => authed(() => engine.runAttack(scenario_id), scenario_id === "all" ? [1400, 1900] : [500, 800]),
  attackRuns: (limit = 10) => authed(() => engine.s.runs.slice(0, limit)),
  scoreboard: () => authed(() => engine.s.scoreboard),

  demoState: () =>
    authed(() => ({
      feed_mode: engine.s.feedMode,
      broker_fault: engine.s.brokerFault,
      scenarios: ["calm", "dip_to_trigger", "spike", "crash", "stale"],
    })),
  demoReset: () =>
    authed(() => {
      engine.reset();
      return { ok: true };
    }, [300, 600]),
  demoFeed: (scenario, symbol) =>
    authed(() => {
      engine.setFeed(scenario, symbol);
      return { ok: true, feed_mode: scenario };
    }),
  demoBrokerFault: (mode, count) =>
    authed(() => {
      engine.s.brokerFault = mode;
      engine.s.faultCount = mode === "none" ? 0 : (count ?? 1);
      engine.audit("user:demo", "DEMO_BROKER_FAULT", "broker", mode);
      return { ok: true, broker_fault: mode };
    }),

  subscribe(handlers) {
    handlers.onStatus("connecting", { attempt: 0 });
    const timer = setTimeout(() => {
      handlers.onStatus("open", { attempt: 0 });
      for (const t of engine.allTicks()) handlers.onEvent({ type: "tick", data: t });
      handlers.onEvent({ type: "monitor", data: engine.s.security.monitor });
    }, 250);
    const unsub = engine.subscribe(handlers.onEvent);
    return () => {
      clearTimeout(timer);
      unsub();
      handlers.onStatus("idle", { attempt: 0 });
    };
  },
};
