// Mock implementation of CountersignApi (NEXT_PUBLIC_MOCK=1). Same interface as the real client.

import type { CountersignApi } from "../api";
import { EMAIL_RE, PASSWORD_MAX, PASSWORD_MIN, weakPinReason } from "../auth";
import { ApiError } from "../errors";
import { link021Codec } from "../link021";
import type { HistoryPoint, Link021Key, Me, OptionChain, PublicStatus, Quote, StepUpMethod, WhatsAppState, WhatsAppWatch } from "../types";
import { chatHistory, simulateChat } from "./chat";
import { MOCK_TOTP, engine, sleep } from "./engine";
import {
  EXPIRIES,
  INSTRUMENTS,
  MOCK_UCC,
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

// Self-service 021 linking. The mock plays the 021 gateway: its own RSA-OAEP key pair (in memory only), single-use
// nonces, and a fresh step-up before linking or unlinking. It never stores or checks a real 021 password.
let mockLinkKeys: Promise<CryptoKeyPair> | null = null;
const linkNonces = new Map<string, number>();
let stepUpUntil = 0;

function linkKeys(): Promise<CryptoKeyPair> {
  mockLinkKeys ??= crypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"],
  );
  return mockLinkKeys;
}

function requireFreshStepUp() {
  const s = engine.s;
  const methods: StepUpMethod[] = [
    ...(s.devices.some((d) => !d.revoked && !d.is_virtual) ? (["webauthn"] as const) : []),
    ...(s.hasPin ? (["pin"] as const) : []),
    ...(s.hasTotp ? (["totp"] as const) : []),
  ];
  if (methods.length && Date.now() > stepUpUntil) {
    throw new ApiError(403, "STEP_UP_REQUIRED", "Confirm it is you with a method you already have, then try again.", undefined, { methods });
  }
}

// WhatsApp alerts. The mock plays the server: Twilio sandbox details, a one-time code, and a log of sent messages.
// Like the API, nothing here can approve, place, modify or cancel a trade.
export const MOCK_WHATSAPP_CODE = "246810";
const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const wa: WhatsAppState & { pendingPhone: string | null } = {
  configured: true,
  mode: "live",
  sandbox: { number: "+1 415 523 8886", join_hint: "join silver-tiger" },
  link: null,
  pendingPhone: null,
  prefs: { orders: true, mandates: true, security: true, digests: false },
  watches: [
    {
      id: "w_tcs",
      symbol: "TCS",
      interval_min: 5,
      min_move_pct: null,
      market_hours_only: true,
      active: true,
      last_sent_at: minutesAgo(3),
      last_price: 4123.4,
    },
    {
      id: "w_itc",
      symbol: "ITC",
      interval_min: 15,
      min_move_pct: 0.5,
      market_hours_only: false,
      active: false,
      last_sent_at: minutesAgo(190),
      last_price: 468.15,
    },
  ],
  recent: [
    { at: minutesAgo(3), kind: "watch", status: "SENT", preview: "TCS 4,123.40 (+0.42% today). Next update in 5 min. Reply UNWATCH TCS to stop." },
    { at: minutesAgo(26), kind: "order", status: "SENT", preview: "Filled: BUY 10 RELIANCE @ 2,931.20. Countersigned with passkey on this account." },
    { at: minutesAgo(48), kind: "command", status: "SENT", preview: "PNL: today +1,284.50 across 6 holdings. Read only: BUY and SELL are refused here." },
    { at: minutesAgo(31 * 60), kind: "digest", status: "FAILED", preview: "Daily digest for 9 Oct could not be delivered (63016 outside the 24 hour window)." },
    { at: minutesAgo(32 * 60), kind: "security", status: "LOGGED", preview: "New sign-in from Chrome on Windows. If this was not you, engage the kill switch in the app." },
  ],
};

// Kept in sessionStorage like the rest of the mock state, so a linked number survives a page load.
const WA_KEY = "cs_mock_whatsapp_v1";
let waLoaded = false;

function waLoad() {
  if (waLoaded) return;
  waLoaded = true;
  try {
    const raw = typeof window === "undefined" ? null : sessionStorage.getItem(WA_KEY);
    if (raw) Object.assign(wa, JSON.parse(raw));
  } catch {
    // storage unavailable: the seed state is used
  }
}

function waSave() {
  try {
    sessionStorage.setItem(WA_KEY, JSON.stringify(wa));
  } catch {
    // ignore
  }
}

/** Runs a WhatsApp mock handler against the stored state and saves what it changed. */
function waDo<T>(fn: () => T): T {
  waLoad();
  const out = fn();
  waSave();
  return out;
}

function waState(): WhatsAppState {
  const { pendingPhone: _pending, ...rest } = wa;
  void _pending;
  return rest;
}

function waLog(kind: string, preview: string) {
  const live = wa.mode === "live" && wa.link?.verified && wa.link.opted_in;
  const paused = wa.link?.paused_until && new Date(wa.link.paused_until).getTime() > Date.now();
  wa.recent.unshift({ at: new Date().toISOString(), kind, status: paused ? "PAUSED" : live ? "SENT" : "LOGGED", preview });
  wa.recent = wa.recent.slice(0, 20);
}

function maskPhone(p: string): string {
  const digits = p.replace(/\D/g, "");
  return `+${digits.slice(0, 2)} ${"•".repeat(5)} ${digits.slice(-4)}`;
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
      stepUpUntil = Date.now() + 10 * 60_000;
      return { ok: true as const, method, expires_at: new Date(stepUpUntil).toISOString() };
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
  brokerStatus: () =>
    authed(() => ({ active_kind: engine.s.activeBroker, gateway021_enabled: true, gateway021: { up: true } }), [50, 120]),
  link021Key: () =>
    authed(async (): Promise<Link021Key> => {
      const pair = await linkKeys();
      const spki = new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey));
      const nonce = `lnk_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
      const expires = Date.now() + 5 * 60_000;
      linkNonces.set(nonce, expires);
      return {
        public_key: link021Codec.bytesToBase64(spki),
        alg: "RSA-OAEP-256",
        nonce,
        user_id: me().user.id,
        expires_at: new Date(expires).toISOString(),
        max_plaintext_bytes: 190,
      };
    }, [80, 180]),
  link021: (ciphertext) =>
    authed(async () => {
      requireFreshStepUp();
      let payload: Record<string, unknown>;
      try {
        const plain = await crypto.subtle.decrypt({ name: "RSA-OAEP" }, (await linkKeys()).privateKey, link021Codec.base64ToBytes(ciphertext));
        payload = JSON.parse(new TextDecoder().decode(plain)) as Record<string, unknown>;
      } catch {
        throw new ApiError(422, "INVALID_INPUT", "The encrypted credentials could not be read. Fetch a new key and try again.");
      }
      const nonce = typeof payload.nonce === "string" ? payload.nonce : "";
      const expires = linkNonces.get(nonce);
      linkNonces.delete(nonce);
      if (!expires || expires < Date.now() || payload.user_id !== me().user.id) {
        throw new ApiError(400, "LINK_NONCE_INVALID", "This link key expired or was already used. Fetch a new one and try again.");
      }
      const ucc = typeof payload.ucc === "string" ? payload.ucc : "";
      if (!/^[A-Z0-9]{4,16}$/.test(ucc)) throw new ApiError(422, "UCC_INVALID", "A 021 UCC has 4 to 16 letters and digits.");
      if (typeof payload.password !== "string" || !payload.password) throw new ApiError(422, "INVALID_INPUT", "Enter your 021 password.");
      if (payload.password === "wrong") throw new ApiError(401, "LINK_WRONG_CREDENTIALS", "Wrong UCC or password");
      if (ucc === MOCK_UCC) throw new ApiError(409, "UCC_LINKED_ELSEWHERE", "This 021 account is already linked to another Countersign user.");
      return engine.linkSelf021(ucc);
    }, [700, 1200]),
  unlink021: (account_id) =>
    authed(() => {
      requireFreshStepUp();
      return engine.unlinkSelf021(account_id);
    }, [250, 450]),

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
      // Cancelling a standing instruction needs a fresh countersignature, like the API.
      requireFreshStepUp();
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

  whatsapp: () => authed(() => waDo(() => waState())),
  whatsappLink: (phone) =>
    authed(() => waDo(() => {
      const clean = phone.replace(/[\s()-]/g, "");
      if (!/^\+[1-9]\d{7,14}$/.test(clean)) {
        throw new ApiError(422, "PHONE_INVALID", "Enter the number in international format, for example +91 98765 43210.");
      }
      wa.pendingPhone = clean;
      return { phone_masked: maskPhone(clean), expires_at: new Date(Date.now() + 10 * 60_000).toISOString() };
    })),
  whatsappVerify: (code) =>
    authed(() => waDo(() => {
      if (!wa.pendingPhone) throw new ApiError(409, "NO_PENDING_LINK", "Send a code to your number first.");
      if (code !== MOCK_WHATSAPP_CODE) throw new ApiError(400, "CODE_INVALID", "That code is not right. Check the latest WhatsApp message and try again.");
      wa.link = { phone_masked: maskPhone(wa.pendingPhone), verified: true, opted_in: true, paused_until: null, window_hint: "Last message from you 2 min ago: delivery window open for about 23 h." };
      wa.pendingPhone = null;
      waLog("system", "Countersign: this number is linked. Alerts only; trades are approved in the app with your passkey, PIN or authenticator code.");
      return { verified: true as const };
    })),
  whatsappUnlink: () =>
    authed(() => waDo(() => {
      wa.link = null;
      wa.pendingPhone = null;
    })),
  whatsappPrefs: (prefs) =>
    authed(() => waDo(() => {
      wa.prefs = { ...prefs };
      return wa.prefs;
    })),
  whatsappPause: (minutes) =>
    authed(() => waDo(() => {
      if (!wa.link) throw new ApiError(409, "NOT_LINKED", "Link a WhatsApp number first.");
      wa.link.paused_until = new Date(Date.now() + minutes * 60_000).toISOString();
    })),
  whatsappResume: () =>
    authed(() => waDo(() => {
      if (wa.link) wa.link.paused_until = null;
    })),
  whatsappAddWatch: (body) =>
    authed(() => waDo((): WhatsAppWatch => {
      const symbol = body.symbol.trim().toUpperCase();
      if (!symbol) throw new ApiError(422, "SYMBOL_REQUIRED", "Pick a stock to watch.");
      if (wa.watches.some((w) => w.symbol === symbol)) throw new ApiError(409, "WATCH_EXISTS", `You already watch ${symbol}. Delete it first to change the interval.`);
      if (wa.watches.length >= 5) throw new ApiError(409, "WATCH_LIMIT", "You can watch up to 5 stocks on WhatsApp.");
      const w: WhatsAppWatch = {
        id: `w_${symbol.toLowerCase()}_${Date.now().toString(36)}`,
        symbol,
        interval_min: body.interval_min,
        min_move_pct: body.min_move_pct ?? null,
        market_hours_only: !!body.market_hours_only,
        active: true,
        last_sent_at: null,
        last_price: null,
      };
      wa.watches.push(w);
      return w;
    })),
  whatsappDeleteWatch: (id) =>
    authed(() => waDo(() => {
      wa.watches = wa.watches.filter((w) => w.id !== id);
    })),
  whatsappSetWatchActive: (id, active) =>
    authed(() => waDo(() => {
      const w = wa.watches.find((x) => x.id === id);
      if (!w) throw new ApiError(404, "NOT_FOUND", `Watch ${id} does not exist.`);
      w.active = active;
      return w;
    })),
  whatsappTest: () =>
    authed(() => waDo(() => {
      if (!wa.link?.verified) throw new ApiError(409, "NOT_LINKED", "Link and verify a WhatsApp number first.");
      waLog("test", "Countersign test message. Reply HELP for commands. BUY and SELL are refused here.");
    })),

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
