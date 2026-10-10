# Countersign API contract (v1)

All endpoints are served by the `api` process (FastAPI, port 8000) under `/api`. The Next.js app proxies `/api/*` to it so cookies are same-origin. All money is **integer paise** (`*_paise`). All timestamps are ISO 8601 UTC strings. Percent-like values are **basis points** (`*_bps`, 100 bps = 1%).

Auth: session cookie `cs_session` (HttpOnly, SameSite=Strict, 12 hours). Every state-changing request (POST/PUT/DELETE) from a signed-in browser must send header `X-CSRF-Token` equal to the value of the readable cookie `cs_csrf`. The unauthenticated sign-in endpoints (signup, login, passkey sign-in) have no session yet, so they are protected against login CSRF by an `Origin` check instead (`403 ORIGIN_REJECTED` for any origin other than `http://localhost:3000` and those listed in `CS_EXTRA_ORIGINS`). Every successful sign-in mints a brand-new session; a presented session is never reused.

Errors use HTTP status codes with body `{ "code": string, "message": string, "detail"?: any }`, plus optional top-level fields where noted: `field` on `422 INVALID_INPUT` (which input to fix) and `retry_after_s` on `429 LOGIN_LOCKED`. Request validation errors are `422 { code: "INVALID_INPUT", field, message }`. Messages are human readable and specific (never "something went wrong").

Every user sees only their own data: account endpoints read the caller's own paper account, and another user's proposals, mandates and conversations answer `404`.

---

## Shared types (TypeScript)

```ts
type Paise = number;
type Bps = number;
type ISO = string;

type Source =
  | "trader_instruction"   // value appears in the trader's own words
  | "instrument_metadata"  // instrument master (lot, tick, freeze, circuit)
  | "live_market_feed"     // tick feed
  | "risk_engine"          // verifier/margin/charges computation
  | "account"              // broker account state (holdings, funds)
  | "system_default"       // safe default applied by the system (e.g. product CNC, validity DAY)
  | "model_inferred";      // value proposed by the AI that the trader never stated (always flagged)

interface InstrumentHandle {
  instrument_id: string;          // stable id, e.g. "NSE:EQ:RELIANCE", "NSE:FO:NIFTY26O1325500CE"
  exchange: "NSE";
  segment: "EQ" | "FO" | "IDX";
  symbol: string;                 // normalized ASCII trading symbol
  display_name: string;           // sanitized, never contains untrusted instruction text
  isin?: string | null;
  expiry?: string | null;         // YYYY-MM-DD for derivatives
  strike_paise?: Paise | null;
  option_type?: "CE" | "PE" | null;
  lot_size: number;
  tick_size_paise: Paise;
  flags: string[];                // e.g. ["LOOKALIKE_SYMBOL", "UNTRUSTED_TEXT_REMOVED", "SUSPENDED"]
}

interface Tick {
  instrument_id: string;
  symbol: string;
  ltp_paise: Paise;
  bid_paise: Paise;
  ask_paise: Paise;
  volume: number;
  prev_close_paise: Paise;
  change_paise: Paise;
  change_bps: Bps;
  ts: ISO;
  seq: number;
  stale: boolean;                 // true if older than the freshness limit (5 s)
}

interface AccountSummary {
  account_id: string;
  day_pnl_paise: Paise;
  total_pnl_paise: Paise;
  cash_paise: Paise;
  available_margin_paise: Paise;
  used_margin_paise: Paise;
  holdings_value_paise: Paise;
  exposure_paise: Paise;
  holdings_count: number;
  positions_count: number;
  open_orders_count: number;
  as_of: ISO;
}

interface Holding {
  instrument: InstrumentHandle;
  quantity: number;
  avg_price_paise: Paise;
  ltp_paise: Paise;
  prev_close_paise: Paise;
  value_paise: Paise;
  pnl_paise: Paise;               // vs average price
  pnl_bps: Bps;
  day_change_paise: Paise;        // (ltp - prev_close) * qty
  day_change_bps: Bps;
}

interface Position {
  instrument: InstrumentHandle;
  product: "CNC" | "MIS" | "NRML";
  quantity: number;               // negative = short
  avg_price_paise: Paise;
  ltp_paise: Paise;
  pnl_paise: Paise;
  pnl_bps: Bps;
  day_pnl_paise: Paise;
}

type BrokerOrderStatus = "OPEN" | "TRIGGER_PENDING" | "PARTIALLY_FILLED" | "COMPLETE" | "REJECTED" | "CANCELLED";

interface BrokerOrder {
  broker_order_id: string;
  client_order_id: string | null;
  instrument: InstrumentHandle;
  side: "BUY" | "SELL";
  quantity: number;
  filled_quantity: number;
  pending_quantity: number;
  order_type: "LIMIT" | "MARKET" | "SL" | "SL-M";
  product: "CNC" | "MIS" | "NRML";
  validity: "DAY" | "IOC";
  limit_price_paise: Paise | null;
  trigger_price_paise: Paise | null;
  protection_price_paise: Paise | null;
  avg_fill_price_paise: Paise | null;
  status: BrokerOrderStatus;
  reject_reason: string | null;
  placed_at: ISO;
  updated_at: ISO;
}

interface OptionLeg { instrument_id: string; ltp_paise: Paise; oi: number; volume: number; iv_bps: Bps; change_bps: Bps }
interface OptionChainRow { strike_paise: Paise; ce: OptionLeg; pe: OptionLeg; is_atm: boolean; distance: number /* strikes from ATM */ }
interface OptionChain {
  underlying: "NIFTY";
  spot_paise: Paise;
  expiry: string;
  expiries: string[];
  atm_strike_paise: Paise;
  strike_step_paise: Paise;
  lot_size: number;
  rows: OptionChainRow[];         // ATM +/- N strikes, sorted ascending
  as_of: ISO;
}
```

### Proposals (the only thing the AI can create)

```ts
type ProposalKind = "ORDER" | "PLAN" | "MANDATE";
type ProposalStatus =
  | "BLOCKED"              // verifier found a blocking issue; cannot be approved
  | "AWAITING_APPROVAL"    // verified; waiting for the trader's countersignature
  | "APPROVED"             // countersigned; capability minted
  | "EXECUTING"
  | "COMPLETED"
  | "PARTIALLY_COMPLETED"
  | "FAILED"
  | "REJECTED_BY_USER"
  | "EXPIRED"
  | "ACTIVE"               // mandate armed
  | "CANCELLED";

interface Issue { code: string; severity: "BLOCK" | "WARN" | "INFO"; message: string; field?: string }

interface FieldRow {           // one row of the approval card, with provenance
  key: string;                 // "side" | "instrument" | "quantity" | "limit_price" | ...
  label: string;
  display: string;             // already formatted for display, e.g. "₹2,801.50"
  source: Source;
  flagged: boolean;
  note?: string;               // e.g. "AI proposed 1000 but you said 100"
}

interface OrderPreview {
  reference_price_paise: Paise;     // LTP at preview time (live_market_feed)
  band_low_paise: Paise;            // approval is void if LTP leaves [low, high]
  band_high_paise: Paise;
  band_bps: Bps;
  worst_case_price_paise: Paise;    // limit price, or protection price for MARKET/SL-M
  order_value_paise: Paise;         // quantity * worst-case price
  estimated_margin_paise: Paise;
  estimated_charges_paise: Paise;
  charges_breakdown: { label: string; paise: Paise }[];
  current_position_qty: number;
  resulting_position_qty: number;
  available_margin_paise: Paise;
  margin_after_paise: Paise;
  exposure_after_paise: Paise;
  tick_age_ms: number;
  session: { open: boolean; label: string };
}

type StepState = "PENDING" | "SUBMITTED" | "OPEN" | "PARTIALLY_FILLED" | "FILLED" | "REJECTED" | "CANCELLED" | "FAILED" | "SKIPPED" | "BLOCKED";

interface CanonicalOrder {
  schema_version: 1;
  action: "PLACE" | "MODIFY" | "CANCEL";
  account_id: string;
  instrument: InstrumentHandle;
  side: "BUY" | "SELL";
  quantity: number;
  order_type: "LIMIT" | "MARKET" | "SL" | "SL-M";
  product: "CNC" | "MIS" | "NRML";
  validity: "DAY" | "IOC";
  limit_price_paise: Paise | null;
  trigger_price_paise: Paise | null;
  protection_price_paise: Paise | null;
  target_broker_order_id: string | null;
}

interface ProposalStep {
  step_no: number;               // 1-based
  canonical: CanonicalOrder;
  summary: string;               // deterministic, e.g. "BUY 100 RELIANCE @ ₹2,800.00 LIMIT (CNC, DAY)"
  fields: FieldRow[];
  preview: OrderPreview | null;  // null for CANCEL
  issues: Issue[];
  depends_on: number | null;     // step that must FILL first
  state?: StepState;
  broker_order_id?: string | null;
  client_order_id?: string | null;
  filled_quantity?: number;
  avg_fill_price_paise?: Paise | null;
  reason?: string | null;
}

interface MandateView {
  mandate_id: string;
  kind: "ORDER" | "ALERT";
  instrument: InstrumentHandle;
  condition_text: string;        // e.g. "LTP <= ₹500.00"
  predicate: { field: "LTP" | "DROP_FROM_AVG_BPS"; op: "LTE" | "GTE"; threshold: number };
  template: CanonicalOrder | null;
  max_quantity: number | null;
  price_limit_paise: Paise | null;
  one_shot: boolean;
  expires_at: ISO;
  state: "PENDING_APPROVAL" | "ACTIVE" | "TRIGGERED" | "EXECUTING" | "FIRED" | "COMPLETED" | "NEEDS_REVIEW" | "EXPIRED" | "CANCELLED" | "FAILED";
  created_at: ISO;
  activated_at?: ISO | null;
  triggered_at?: ISO | null;
  fired_at?: ISO | null;
  trigger_event_id?: string | null;
  last_evaluated_at?: ISO | null;
  reason?: string | null;
}

interface ApprovalState {
  approval_id: string;
  status: "CHALLENGED" | "VERIFIED" | "FAILED" | "EXPIRED";
  method: "webauthn" | "pin" | "totp";
  assurance: "device_signed" | "server_attested";
  issued_at: ISO;
  expires_at: ISO;
  verified_at?: ISO | null;
  credential_label?: string | null;
  payload_hash: string;
  failure_reason?: string | null;
}

interface ExecutionState {
  job_id: string;
  status: "PENDING" | "HELD" | "RUNNING" | "DONE" | "REJECTED" | "HALTED";
  reason?: string | null;
  outbox: { client_order_id: string; step_no: number; state: string; broker_order_id: string | null; attempts: number; reconciled: boolean }[];
}

interface Proposal {
  id: string;
  kind: ProposalKind;
  status: ProposalStatus;
  summary: string;               // deterministic human-readable summary of the canonical object
  canonical_hash: string;        // sha256 of RFC 8785 canonical JSON
  short_hash: string;            // first 10 hex chars, shown as the order/plan id
  steps: ProposalStep[];         // ORDER = 1 step, PLAN = n steps, MANDATE = template step (may be empty for alerts)
  mandate: MandateView | null;
  failure_policy: "STOP_ON_FAILURE" | null;
  issues: Issue[];               // aggregate
  can_approve: boolean;
  interpretation: {
    user_text: string;
    engine: string;              // "deterministic" | "ollama:<model>" | "gemini:<model>" | "groq:<model>" | "compromised-model"
    disagreements: { field: string; trader_value: string; model_value: string }[];
  };
  approval: ApprovalState | null;
  execution: ExecutionState | null;
  created_at: ISO;
  updated_at: ISO;
}
```

---

## Endpoints

### Accounts and sign-in (no session needed)
| Method | Path | Body | Returns | Errors |
|---|---|---|---|---|
| POST | `/api/auth/signup` | `{ display_name: string /* 1-64 */; email: string; password: string /* 8-128, not the email */ }` | `201 Me` + fresh `cs_session` / `cs_csrf` cookies. Provisions a new paper trading account (`PT` + 6 base32 characters) seeded like the demo portfolio. | `409 EMAIL_TAKEN` (case-insensitive), `422 INVALID_INPUT` with `field`, `429 RATE_LIMITED`, `403 ORIGIN_REJECTED`, `503 BROKER_UNAVAILABLE` or `PROVISIONING_FAILED` |
| POST | `/api/auth/login` | `{ email: string; password: string }` | `200 Me` + fresh cookies | `401 INVALID_CREDENTIALS` ("Email or password is incorrect.", identical for unknown emails), `429 LOGIN_LOCKED` with `retry_after_s` and a `Retry-After` header, `429 RATE_LIMITED`, `403 ORIGIN_REJECTED` |
| POST | `/api/auth/passkey/login/start` | `{}` | `{ challenge_id: string; options: PublicKeyCredentialRequestOptionsJSON }` (discoverable: empty `allowCredentials`, `userVerification: "required"`, `rpId` from config) | `429`, `403` |
| POST | `/api/auth/passkey/login/finish` | `{ challenge_id: string; credential: AuthenticationResponseJSON }` | `200 Me` + fresh cookies; the device sign counter is updated | `401 PASSKEY_NOT_RECOGNIZED` (unknown, revoked, Attack Lab virtual device or wrong user handle), `401 SIGNATURE_INVALID`, `410 CHALLENGE_EXPIRED` (each challenge works once, for 3 minutes), `429`, `403` |
| POST | `/api/auth/demo-login` | `{ username?: string }` | `Me`. **Automation only** (smoke and flow scripts, tests), demo mode only; the web UI never calls it. Only for scripts on this machine calling the API directly: `403` for anything that came through the web proxy (`X-Forwarded-For` or `X-Forwarded-Host`), a foreign `Origin` or a non-loopback peer. `404` for any account that has an email, `403` for the Attack Lab identity. | |

Login lockout, per (email, client): the first 4 wrong passwords answer `401`; the 5th and later lock that email's password sign-in on that client for 30 s, doubling per further failure up to 15 minutes (`429 LOGIN_LOCKED` with `retry_after_s`). Failures are forgotten after 15 quiet minutes; a passkey sign-in clears every lock on the account; 50 failures inside the window from any number of clients lock the account everywhere for 15 minutes. Unknown emails get the same responses, the same lockout and the same Argon2 cost (also while locked), so sign-in never reveals whether an email is registered; sign-up does (`409 EMAIL_TAKEN`), by design. Signing in, with a password or a passkey, is **not** a trade approval: every trade still needs a fresh countersignature.

### Session, factors and devices (signed in)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/auth/logout` | | `{ ok: true }` (revokes the session and clears the cookies) |
| GET | `/api/me` | | `Me` |
| GET | `/api/health` | | `{ ok: true; db: boolean; valkey: boolean; holds_write_key: false; holds_021_credentials: false; gateway021_enabled: boolean }` (no auth; the monitor checks `holds_write_key` and `holds_021_credentials` every second) |
| POST | `/api/auth/step-up/start` | `{}` | `{ challenge_id: string; options: PublicKeyCredentialRequestOptionsJSON }` listing the caller's active passkeys (user verification required). `412 NO_DEVICE_REGISTERED` without one |
| POST | `/api/auth/step-up` | `{ method: "pin" \| "totp" \| "webauthn"; pin?: string; code?: string; challenge_id?: string; credential?: AuthenticationResponseJSON }` | `{ ok: true; method; expires_at: ISO }`: this session may change approval methods for 10 minutes. Never a trade approval. Errors: `401 PIN_INVALID`, `423 PIN_LOCKED`, `401 TOTP_INVALID` or `TOTP_REPLAY`, `401 SIGNATURE_INVALID`, `410 CHALLENGE_EXPIRED`, `422 METHOD_INVALID`, `429 RATE_LIMITED` |
| POST | `/api/auth/webauthn/register/start` | `{ device_label?: string /* up to 48 */ }` | `PublicKeyCredentialCreationOptionsJSON` (platform authenticator, discoverable credential, user verification required, user name = email). `403 STEP_UP_REQUIRED` with `methods` once the trader has any approval method and this session has no fresh step-up |
| POST | `/api/auth/webauthn/register/finish` | `RegistrationResponseJSON` | `Device` (same step-up rule). Listed as `PASSKEY_REGISTERED` in the trader's Security events |
| POST | `/api/auth/pin` | `{ pin: string /* 4 ASCII digits */, current_pin?: string /* required to change an existing PIN */ }` | `{ ok: true }`. `422 WEAK_PIN` for one repeated digit (1111) or a straight run up or down (1234, 0123, 4321, 9876; no wrap-around), `422 PIN_FORMAT` (anything but 4 ASCII digits). Choosing the first approval method, or changing the PIN with the current one, counts as a step-up for this session |
| POST | `/api/auth/pin/reset` | `{ password: string; new_pin: string; totp?: string }` | `{ ok: true }`. PIN recovery: the account password plus, when the trader has one, a second method that is not the PIN: the current authenticator code in `totp`, or a passkey proved through `/api/auth/step-up` in the last 10 minutes. Sets the new PIN, clears any PIN lockout, signs out every other session, audits `PIN_RESET`. Errors: `401 INVALID_CREDENTIALS` (also for passwordless demo accounts), `422 WEAK_PIN` or `PIN_FORMAT`, `422 TOTP_REQUIRED` (authenticator linked), `422 PASSKEY_REQUIRED` (passkey, no authenticator), `422 TOTP_INVALID` (a replayed code too), `429 RATE_LIMITED` |
| POST | `/api/auth/totp/setup` | | `{ otpauth_uri: string; secret: string }` (the label in the authenticator app is the email). The new secret stays pending: a linked authenticator keeps working until the new one is confirmed. `403 STEP_UP_REQUIRED` as for passkey registration |
| POST | `/api/auth/totp/verify` | `{ code: string }` | `{ ok: true }`; confirms the pending secret, which then replaces any previous one (`AUTHENTICATOR_LINKED` or `AUTHENTICATOR_REPLACED` in Security events) |
| DELETE | `/api/auth/devices/{id}` | | `{ ok: true }` (revoke) |
| GET | `/api/sessions` | | `SessionInfo[]` |
| DELETE | `/api/sessions/{id}` | | `{ ok: true }` (revoke) |

```ts
interface Device { id: string; label: string; created_at: ISO; last_used_at: ISO | null; revoked: boolean; is_virtual: boolean; transports: string[] }
interface Me {
  user: { id: string; username: string; display_name: string };
  email: string | null;                       // null for the automation-only demo account
  account_id: string;                         // the caller's paper trading account, e.g. "PTZWMTPI" ("DEMO1" for the demo account)
  account: { id: string; label: string };     // "Paper trading account" ("Demo Trader" for DEMO1)
  devices: Device[];
  has_pin: boolean;
  has_totp: boolean;
  pin_locked_until: ISO | null;
  session: { id: string; created_at: ISO; expires_at: ISO };
  onboarding: { pin_set: boolean; passkeys: number /* active, not virtual */; totp: boolean; complete: boolean /* === pin_set */ };
  demo_mode: boolean;                         // demo controls are available
}
interface SessionInfo { id: string; created_at: ISO; expires_at: ISO; user_agent: string; current: boolean; revoked: boolean }
```

Example (`POST /api/auth/signup`, abbreviated):
```json
{"user":{"id":"df6a7aad-...","username":"asha@example.test","display_name":"Asha Rao"},"email":"asha@example.test",
 "account_id":"PTZWMTPI","account":{"id":"PTZWMTPI","label":"Paper trading account"},"devices":[],"has_pin":false,
 "has_totp":false,"pin_locked_until":null,"session":{"id":"bf20...","created_at":"...Z","expires_at":"...Z"},
 "onboarding":{"pin_set":false,"passkeys":0,"totp":false,"complete":false},"demo_mode":true}
```

### Rate limits
Sliding one-minute windows, answered with `429 RATE_LIMITED`, `{ code, message: "Too many attempts. Wait N s and try again.", retry_after_s: N }` and a matching `Retry-After` header. Each rule matches its exact path (only the approval rule matches a path fragment), and only the first matching rule applies. Signed-in endpoints are keyed by session. Sign-in endpoints are keyed by the client's TCP address: `X-Forwarded-For` is ignored, because the Next.js proxy passes a browser-supplied value through unchanged. Behind the local proxy every browser therefore shares the loopback bucket.

| Endpoint | Limit | Keyed by |
|---|---|---|
| `POST /api/chat` | 20/min | session |
| `POST .../approval/start`, `.../approval/complete` | 12/min | session |
| `POST /api/auth/pin` | 10/min | session |
| `POST /api/auth/pin/reset` | 5/min | session |
| `POST /api/auth/step-up`, `/api/auth/step-up/start` | 10/min each | session |
| `POST /api/attack-lab/run` | 6/min | session |
| `POST /api/auth/signup` | 5/min | IP |
| `POST /api/auth/login` | 10/min | IP |
| `POST /api/auth/passkey/login/start`, `/finish` | 10/min each | IP |
| `GET /api/public/status` | 60/min | IP |
| `POST /api/whatsapp/link` | 5 per 10 min (and 3 codes per 10 min per user) | session |
| `POST /api/whatsapp/verify` | 10/min (and 5 attempts per code) | session |
| `POST /api/whatsapp/test` | 5/min | session |
| `POST /api/whatsapp/inbound` | 120/min | IP |

### Public status (no auth, for the landing page)
`GET /api/public/status` returns system-wide facts only, never per-user data. Cached for about 2 s.
```ts
interface PublicStatus {
  all_operational: boolean;
  systems: { api: boolean; broker: boolean; feed: boolean; executor: boolean; monitor: boolean };
  unapproved_executions: number;        // from the independent monitor
  attacks_attempted: number;            // Attack Lab, all runs
  attacks_blocked: number;
  last_attack_run_at: ISO | null;
  audit_events: number;
  market: { mode: "replay" | "live"; nifty: { ltp_paise: Paise; change_bps: Bps } | null };   // "live" while the 021 gateway runs
  generated_at: ISO;
}
```

### Account (read-only gateway; uses the broker READ key only)
Every read uses the caller's **active** account: their paper account, or a 021 account they may operate (the operator-linked account when they are a listed owner, or a 021 account they connected themselves) when it is active.

| GET | `/api/account/summary` | `AccountSummary` |
|---|---|---|
| GET | `/api/account/holdings` | `Holding[]` (021 adds `free_quantity`, `btst_quantity`, `sell_quantity`, `today_quantity`, `prev_close_paise`) |
| GET | `/api/account/positions` | `Position[]` (021 adds `square_off_quantity`, `prev_close_paise`; 021's INTRADAY is shown as MIS) |
| GET | `/api/account/orders?status=open\|all\|rejected` | `BrokerOrder[]` (021 adds `status_021`, `price_021_paise`, `trigger_021_paise`, `token_021`, `exchange_021`, `exchange_order_number`, `side_known` and `origin`: `"countersign"` or `"external"` for orders placed outside Countersign) |
| GET | `/api/account/funds` | `{ estimate: boolean; cash_paise; used_margin_paise; available_paise; basis?: string; account_id }`. On 021 `estimate` is `true` (021 has no funds API) and `basis` explains the estimate; `account_id` is masked there |

### Broker accounts (021 linking)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/broker/accounts` | | `BrokerAccount[]`: the caller's paper account, the operator-linked 021 account for users listed in `CS_021_OWNER_EMAILS`, and every 021 account the caller connected themselves |
| POST | `/api/broker/active` | `{ account_id: string }` | `{ active_account_id: string; accounts: BrokerAccount[] }`; `404 ACCOUNT_NOT_FOUND` if the caller cannot operate that account (also when their email was removed from the owner list, or after they disconnected it) |
| GET | `/api/broker/status` | | `{ active_kind: "paper" \| "021"; gateway021_enabled: boolean; gateway021: null \| { up: boolean; logged_in?; token_expires_at?; account? /* masked */; instruments?; market_socket?; orders_socket?; http? /* 429, 5xx, timeouts, read retries, re-logins */; last_reconciliation? } }`. `logged_in`, `token_expires_at` and `account` describe the caller's own 021 account (the active one, else the first they may operate). Never contains a credential, a token or the unmasked account id |
| GET | `/api/broker/021/link-key` | | `{ public_key: string /* SPKI DER, base64 */; alg: "RSA-OAEP-256"; nonce: string; user_id: string; expires_at: ISO; max_plaintext_bytes: 190 }`. The nonce is single use, lives 5 minutes and is stored server side; asking again retires any earlier unused nonce. `503 GATEWAY021_OFF` when the gateway is not running, `503 GATEWAY021_UNAVAILABLE` when it does not answer |
| POST | `/api/broker/021/link` | `{ ciphertext: string }` | Self-service linking. `ciphertext` is the standard base64 of WebCrypto RSA-OAEP (SHA-256) over the UTF-8 JSON `{ ucc, password, nonce, user_id }`, encrypted to `public_key` with the nonce and user id from `link-key`. Returns `{ masked_ucc: string; account: BrokerAccount; accounts: BrokerAccount[]; active_account_id: string }` and the linked account becomes active. Needs a fresh step-up (`403 STEP_UP_REQUIRED` with `methods`; with no approval method yet, `methods` is empty). Errors: `400 LINK_NONCE_INVALID` (no unused nonce younger than 5 minutes), `400 LINK_NONCE_MISMATCH` / `LINK_REPLAYED` / `LINK_DECRYPT_FAILED` (from the gateway), `401 LINK_WRONG_CREDENTIALS` (021 answered 401 or 403), `422 UCC_INVALID` (4 to 16 letters or digits) / `PASSWORD_INVALID` / `LINK_REFUSED` (another 4xx from 021), `503 LINK_021_UNAVAILABLE` ("021 did not answer, try again": 429, 5xx or timeout), `409 UCC_LINKED_ELSEWHERE` (already connected by another Countersign user, refused by the gateway before any 021 login; the current `.env` account is the exception, see below), `409 UCC_CONFLICT` (the id belongs to a paper account), `409 LINK_EXPIRED` (the gateway deleted the pending link before the API confirmed it), `429 LINK_TOO_MANY_FAILURES` ("Too many failed attempts for this account, try again later.": 3 failed verification logins for this UCC in 24 hours, across all users; 021 is not contacted), `429 RATE_LIMITED` (`retry_after_s`; 5 attempts per user in 15 minutes, 10 per user in 24 hours and 60 for all users together in 15 minutes, counted after the step-up check; there is no per-IP bucket), `500 LINK_FAILED` (an error after the gateway verified; the gateway was told to delete the link, nothing was kept), `503 LINK_ROLLBACK_FAILED` (that error, and the gateway could not be told; it deletes an unconfirmed link on its own within 2 minutes). Messages about 021's answer are fixed texts with the HTTP status only. Every refusal is audited with its `reason`. Nothing is stored unless 021 accepted the password and the API confirmed the link |
| DELETE | `/api/broker/021/link` | `{ account_id: string }` | `{ ok: true; active_account_id: string; accounts: BrokerAccount[] }`. Disconnects a 021 account the caller connected: the gateway deletes the stored credential (and drops that account's session when no other trader linked it; the `.env` account keeps its session), the link is removed and the caller is moved back to their paper account. Needs a fresh step-up. `404 ACCOUNT_NOT_FOUND` (not connected by the caller), `409 LINK_OPERATOR_MANAGED` (the operator-linked account), `503 GATEWAY021_UNAVAILABLE` (the gateway could not delete the credential after two tries; nothing was changed and the account stays connected; audited as `ACCOUNT_UNLINK_021_FAILED`) |

```ts
interface BrokerAccount {
  account_id: string;            // for POST /api/broker/active and DELETE /api/broker/021/link only; display label or masked_id
  kind: "paper" | "021";
  label: string;                 // "Paper trading account" | "021 sandbox HACK***3"
  masked_id: string;
  active: boolean;
  linked_by: "self" | "operator" | null;   // 021 only: connected by this trader, or by the operator (CS_021_OWNER_EMAILS)
  status: { connected: boolean; token_expires_at: ISO | null; market_data: "live" | "replay"; last_error: string | null };
}
```
A UCC already connected by another Countersign user is refused, except the gateway's current `.env` account (compared after trimming and upper-casing): a trader who proves its password is linked to it as a member (the listed owners keep it). Listed owners operate only that account; a 021 row left as `operator` by an earlier `.env` UCC grants them nothing and is switched to `self`. The API never sees the UCC or password in clear: it relays the ciphertext with the nonce it issued and the caller's user id to `gateway021` (`POST /admin/link`, admin key, loopback only), which keeps the verified password as a pending link; the API attaches the account and confirms inside one transaction (`POST /admin/link/confirm`), and on any failure asks the gateway to delete the link (`POST /admin/unlink` with `pending_only`, tried twice). The gateway deletes links not confirmed within 2 minutes. It never logs or stores the ciphertext. Link, refusal and unlink are audited with the masked UCC only (`ACCOUNT_LINKED_021`, `ACCOUNT_LINK_021_REFUSED`, `ACCOUNT_UNLINKED_021`) and listed in the trader's Security events.
`GET /api/me` also returns `account: { id, label, kind }` for the active account, and `GET /api/security/status` adds `kind` and `label` to `broker`.

### Market
| GET | `/api/market/instruments?q=` | `InstrumentHandle[]` |
|---|---|---|
| GET | `/api/market/quote/{symbol}` | `{ instrument: InstrumentHandle; tick: Tick }` |
| GET | `/api/market/watchlist` | `{ instrument: InstrumentHandle; tick: Tick }[]` |
| GET | `/api/market/history/{symbol}?points=300` | `{ time: number /* unix s */; value: number /* rupees */ }[]` |
| GET | `/api/market/option-chain?underlying=NIFTY&expiry=YYYY-MM-DD&width=8` | `OptionChain`. With the 021 gateway on it is built from live 021 prices: `iv_bps` is `null` and a leg's `ltp_paise` is `null` until 021 has sent a price for it (never a simulated price) |

### Chat (Server-Sent Events over POST)
`POST /api/chat` with `{ conversation_id?: string; message: string; engine?: "auto" | "ollama" | "gemini" | "groq" | "deterministic" }` returns `text/event-stream`. Each SSE `data:` line is one JSON object:

```ts
type ChatEvent =
  | { type: "meta"; conversation_id: string; engine: string; message_id: string }
  | { type: "tool"; id: string; name: string; label: string; status: "running" | "done" | "error"; detail?: string }
  | { type: "text-delta"; delta: string }
  | { type: "card"; card: Card }
  | { type: "done" }
  | { type: "error"; code: string; message: string };

type Card =
  | { kind: "account_summary"; data: AccountSummary }
  | { kind: "holdings"; title: string; data: Holding[] }
  | { kind: "positions"; title: string; data: Position[] }
  | { kind: "orders"; title: string; data: BrokerOrder[] }
  | { kind: "quote"; data: { instrument: InstrumentHandle; tick: Tick } }
  | { kind: "option_chain"; data: OptionChain }
  | { kind: "proposal"; proposal_id: string; status: ProposalStatus }   // UI fetches GET /api/proposals/{id}; never renders AI text as the order
  | { kind: "clarify"; question: string; options: string[] }
  | { kind: "notice"; tone: "info" | "warning" | "danger" | "success"; title: string; message: string };
```

`GET /api/chat/history?conversation_id=` returns `{ id: string; role: "user" | "assistant"; text: string; cards: Card[]; created_at: ISO }[]`.

### Proposals and countersignature
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/proposals?kind=&status=&limit=` | | `Proposal[]` |
| GET | `/api/proposals/{id}` | | `Proposal` |
| POST | `/api/proposals/{id}/reject` | | `Proposal` |
| POST | `/api/proposals/{id}/approval/start` | `{ method: "webauthn" \| "pin" \| "totp" }` | `ApprovalStart` |
| POST | `/api/proposals/{id}/approval/complete` | `{ approval_id: string; webauthn?: AuthenticationResponseJSON; pin?: string; totp?: string }` | `Proposal` (status `APPROVED` or `ACTIVE` for mandates) |

```ts
interface ApprovalStart {
  approval_id: string;
  issued_at: ISO;
  expires_at: ISO;               // issued_at + 60 s
  payload: Record<string, unknown>;   // the exact object being signed (canonical hash, band, nonce, expiry, account)
  payload_hash: string;          // sha256(RFC 8785(payload)); this IS the WebAuthn challenge
  webauthn: PublicKeyCredentialRequestOptionsJSON | null;  // for @simplewebauthn/browser startAuthentication({ optionsJSON })
}
```

A price standing instruction whose condition is already true at the live price is answered with a `clarify` card instead of a proposal: the question states the live price, and `options` are complete sentences (the order now as a limit order, and a trigger about 2% beyond the live price) that can be sent back as the next message.

Approval errors (HTTP 409/412/422/423) use codes: `APPROVAL_EXPIRED`, `PRICE_DRIFT`, `STALE_MARKET_DATA`, `SIGNATURE_INVALID`, `PAYLOAD_MISMATCH`, `PIN_INVALID`, `PIN_LOCKED`, `TOTP_INVALID`, `PROPOSAL_BLOCKED`, `ALREADY_APPROVED`, `KILL_SWITCH_ENGAGED`, `NO_DEVICE_REGISTERED`.

### Mandates, plans, outcomes
| GET | `/api/mandates` | `MandateView[]` (with `proposal_id`) |
|---|---|---|
| POST | `/api/mandates/{id}/cancel` | `MandateView`. Needs a fresh step-up in this session (`POST /api/auth/step-up` within 10 minutes, the same rule as adding a factor or linking 021); otherwise `403 { code: "STEP_UP_REQUIRED", message, methods }` and nothing changes |
| GET | `/api/plans` | `Proposal[]` (kind PLAN) |
| GET | `/api/outcomes?limit=50` | `OutcomeEvent[]` |

```ts
interface OutcomeEvent {
  id: string; ts: ISO;
  kind: "ORDER_ACCEPTED" | "ORDER_PARTIALLY_FILLED" | "ORDER_FILLED" | "ORDER_REJECTED" | "ORDER_CANCELLED"
      | "EXECUTION_BLOCKED" | "RECONCILED" | "MANDATE_ARMED" | "MANDATE_TRIGGERED" | "MANDATE_NEEDS_REVIEW" | "ALERT"
      | "ORDER_NOT_PLACED" | "ORDER_NEEDS_REVIEW" | "EXTERNAL_ORDER"     // 021: not sent (countersign again), lost answer that needs review, order placed outside Countersign
      | "PLAN_COMPLETED" | "PLAN_PARTIALLY_COMPLETED" | "PLAN_FAILED" | "APPROVAL_VERIFIED" | "KILL_SWITCH";
  title: string; detail: string;
  severity: "info" | "success" | "warning" | "danger";
  proposal_id?: string | null; broker_order_id?: string | null;
}
```

### WhatsApp alerts (Twilio; notification and read-only channel)
WhatsApp can never approve, place, modify or cancel anything. All routes below use the session cookie, and the unsafe methods need `X-CSRF-Token` and an allowed Origin like every other route, except the Twilio webhook.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/whatsapp` | | `WhatsAppState` (always 200 for a signed-in trader, also in log-only mode or with the notifier down) |
| POST | `/api/whatsapp/link` | `{ phone }` (E.164, spaces allowed; India: `+91` and a 10-digit mobile) | `{ phone_masked, expires_at, delivery: Delivery }`; `422 INVALID_PHONE`, `409 PHONE_IN_USE`, `429 RATE_LIMITED` (3 codes per 10 min) |
| POST | `/api/whatsapp/verify` | `{ code }` (6 digits) | `{ verified: true, welcome: Delivery }`; `401 CODE_INVALID { attempts_left }`, `404 NO_CODE`, `410 CODE_EXPIRED`, `429 CODE_LOCKED`, `409 PHONE_IN_USE` |
| DELETE | `/api/whatsapp/link` | | `{ unlinked: true }` (stops every message) |
| POST | `/api/whatsapp/pause` | `{ minutes }` (1 to 10080) | `{ paused_until, link }`; alerts and watches are held back until then (held-back alerts are listed as `PAUSED`, never sent later); `409 NOT_LINKED` |
| POST | `/api/whatsapp/resume` | | `{ paused_until: null, link }`; `409 NOT_LINKED` |
| PUT | `/api/whatsapp/prefs` | `{ orders, mandates, security, digests }` (all booleans) | `Prefs` |
| POST | `/api/whatsapp/watches` | `{ symbol, interval_min, min_move_pct?, market_hours_only? }` | `Watch`; `422 BAD_INTERVAL` / `UNKNOWN_SYMBOL` / `BAD_MIN_MOVE`, `409 WATCH_LIMIT` (10 per user). The same symbol again updates its watch |
| PATCH | `/api/whatsapp/watches/{id}` | `{ active }` | `Watch` |
| DELETE | `/api/whatsapp/watches/{id}` | | `{ deleted: true }` |
| POST | `/api/whatsapp/test` | | `Delivery` ("Countersign test message: alerts are working."); `409 NOT_VERIFIED` |
| POST | `/api/whatsapp/inbound` | Twilio webhook, `application/x-www-form-urlencoded` | TwiML `<Response><Message>...</Message></Response>`. No session and no CSRF: the `X-Twilio-Signature` header is checked by the notifier against `CS_WHATSAPP_PUBLIC_URL`; missing or wrong is `403`, notifier down or log-only mode is `503` |

```ts
interface WhatsAppState {
  configured: boolean;                 // Twilio settings present in the notifier
  mode: "live" | "log_only";           // log_only: messages are recorded, not sent
  notifier_up: boolean;                // the notifier process answered
  sandbox: { number: string;           // "+1 415 523 8886" (any other sender is masked)
             join_hint: string };      // "Send 'join <your code>' to the sandbox number from WhatsApp (find the code in the Twilio console)"
  link: null | { phone_masked: string; // "+91******3210"
                 verified: boolean; opted_in: boolean;  // opted_in false after the trader sent STOP
                 paused_until: ISO | null;              // PAUSE <N>H
                 window_hint: string | null };          // set after Twilio 63016 until the trader messages the sandbox again
  prefs: Prefs;
  watches: Watch[];
  recent: { at: ISO; kind: string; status: MessageStatus; preview: string; error_code: number | null; hint: string | null }[]; // last 10 sent or held back (inbound commands are not listed; their replies are, as SENT)
}
interface Prefs { orders: boolean; mandates: boolean; security: boolean; digests: boolean }  // defaults true, true, true, false
interface Watch { id: string; symbol: string; interval_min: 2 | 5 | 10 | 15 | 30; min_move_pct: number | null;
                  market_hours_only: boolean; active: boolean; last_sent_at: ISO | null; last_price: number | null } // rupees
interface Delivery { status: MessageStatus | "NOT_LINKED" | "NOT_VERIFIED" | "OPTED_OUT"; hint: string | null; message_id: string | null; error_code: number | null }
type MessageStatus =
  | "SENT"           // accepted by Twilio (the notifier reads the delivery status back for 10 minutes and may turn it into a failure)
  | "LOGGED"         // log-only mode: recorded, not sent
  | "FAILED_WINDOW"  // Twilio 63016: "Send any message to the sandbox number to reopen the 24-hour window."
  | "FAILED"         // anything else; error_code and hint say why: 63015 not joined the sandbox ("join <code>"), 63007 wrong
                     // sender, 21211 / 21614 / 63003 bad number, 20003 wrong Twilio credentials, 429 this installation's own
                     // rate limit (20 per user and 300 in total per 10 minutes), no code: notifier down or network error
  | "PAUSED";        // an alert held back while paused (PAUSE <N>H or POST /pause); never sent later
// A failed send is never retried.
```

Inbound commands (case-insensitive, read-only): `HELP`, `PNL`, `HOLDINGS`, `POSITIONS`, `ORDERS`, `PRICE <SYMBOL>`, `MANDATES`, `WATCH <SYMBOL> <MINUTES> [MIN%]`, `UNWATCH <SYMBOL>`, `WATCHES`, `PAUSE <N>H` (1 to 72), `RESUME`, `STOP` (opt out), `START` (opt back in). Trading verbs (`BUY`, `SELL`, `CANCEL`, `APPROVE`, `MODIFY`, `YES` and similar) are answered with "WhatsApp can't place or approve orders. Open Countersign to draft and countersign: https://countersign.everyrepo.me/copilot". A sender that is not a verified link is told how to link the number in the app.

### Live events (SSE, GET)
`GET /api/events` streams `event: <type>` + `data: <json>`:
`tick` (Tick, throttled per instrument), `outcome` (OutcomeEvent), `proposal` (`{ id, status }`), `mandate` (MandateView), `security` (SecurityEvent), `monitor` (MonitorStatus), `audit` (`{ id, event_type, ts }`), `ping`.
The stream is filtered per user: proposal, mandate, order, plan and outcome events reach only their owner (events without owner fields are dropped). Ticks, monitor, security and system-wide outcomes (such as the kill switch) go to everyone.

### Audit and security center
| GET | `/api/audit?limit=100&before_id=` | `AuditEvent[]`: the caller's own events plus system subjects (security, invariant, system, Attack Lab runs, feed, broker) and the shared Attack Lab identity |
|---|---|---|
| GET | `/api/audit/verify` | `{ ok: boolean; checked: number; head_hash: string; broken_at_id: number \| null; detail: string }` (always verifies the whole chain) |
| GET | `/api/security/status` | `SecurityStatus` |
| GET | `/api/security/events?scope=mine\|all` | `SecurityEvent[]` with `scope: "account" \| "system" \| "drill"`. `mine` (default): events about the caller's own account plus system-wide ones; `all` also lists the drills on the shared Attack Lab sandbox (refusals there are stored resolved). Other traders' accounts are never listed |
| POST | `/api/security/kill-switch` | `{ engaged: boolean; pin?: string }` -> `SecurityStatus`. The kill switch is global. Disengaging always requires the caller's own PIN (`422 PIN_NOT_SET` without one, `PIN_INVALID` or `PIN_LOCKED` on a wrong one). |

```ts
interface AuditEvent { id: number; ts: ISO; actor: string; event_type: string; subject_type: string; subject_id: string; data: Record<string, unknown>; prev_hash: string; hash: string }
interface SecurityEvent { id: string; ts: ISO; severity: "info" | "warning" | "critical"; kind: string; message: string; data: Record<string, unknown>; resolved: boolean; scope: "account" | "system" | "drill" }
interface MonitorCheck { name: string; ok: boolean; detail: string; checked: number }
interface MonitorStatus { healthy: boolean; last_heartbeat: ISO | null; checks: MonitorCheck[]; violations: number; unapproved_executions: number }
interface SecurityStatus {
  kill_switch: { engaged: boolean; reason: string | null; at: ISO | null };
  monitor: MonitorStatus;
  executor: { healthy: boolean; last_heartbeat: ISO | null; holds_write_key: true };
  api: { holds_write_key: false; llm_has_order_tools: false };
  broker: { healthy: boolean; latency_ms: number | null };
  feed: { healthy: boolean; last_tick_age_ms: number | null; mode: string };
  llm: { engine: string; available: { ollama: boolean; gemini: boolean; groq: boolean; deterministic: true } };
}
```

### Attack Lab
| GET | `/api/attack-lab/scenarios` | `Scenario[]` |
|---|---|---|
| POST | `/api/attack-lab/run` | `{ scenario_id: string \| "all" }` -> `AttackRun` |
| GET | `/api/attack-lab/runs?limit=10` | `AttackRun[]` |
| GET | `/api/attack-lab/scoreboard` | `Scoreboard` |

```ts
interface Scenario { id: string; letter: string; title: string; attack: string; expected_guarantee: string }
interface TranscriptLine { t_ms: number; actor: "attacker" | "model" | "verifier" | "approval" | "executor" | "broker" | "monitor" | "naive-copilot"; step: string; outcome: "attempted" | "blocked" | "allowed" | "info" }
interface ScenarioResult {
  scenario_id: string; letter: string; title: string;
  status: "BLOCKED" | "FAILED_OPEN" | "ERROR";
  guarantee: string; verdict: string;
  attempted_actions: number; blocked_actions: number;
  unapproved_orders: number;           // orders that reached the broker without a valid countersignature (must be 0)
  naive_unapproved_orders: number;     // what a naive "LLM + confirm button" copilot did on its sandbox account
  transcript: TranscriptLine[];
  duration_ms: number;
}
interface AttackRun { id: string; started_at: ISO; finished_at: ISO; results: ScenarioResult[]; scoreboard: Scoreboard }
interface Scoreboard { unapproved_executions: number; naive_unapproved_executions: number; attacks_attempted: number; attacks_blocked: number; last_run_at: ISO | null }
```

### Demo controls (enabled only when `CS_DEMO_MODE=true`, and only on this machine)
Every `/api/demo/*` route answers `404` unless the request comes from a loopback peer with a loopback `Host` and `X-Forwarded-Host` and no Cloudflare tunnel headers, so the public URL cannot reach them by any path spelling. The paper broker caps a fault at 5 orders.

| POST | `/api/demo/reset` | `{ ok: true; account_id: string }`. Answers `409 NOT_RESETTABLE` when the caller's active account is the 021 sandbox account. Resets **only the caller**: their paper account at the broker (orders and trades deleted; cash, holdings and position reseeded) and their own proposals, approvals and execution records, mandates, plans, outcomes, order mirrors and chat. Also sets the feed to `calm`, clears broker faults and refills the LAB1 and NAIVE1 sandboxes. Never touches other users, the kill switch, security events or the audit chain. |
|---|---|---|
| POST | `/api/demo/feed` | `{ scenario: "calm" \| "dip_to_trigger" \| "spike" \| "crash" \| "stale"; symbol?: string }` |
| POST | `/api/demo/broker-fault` | `{ mode: "none" \| "accept_then_drop" \| "delay_ack" \| "reject" \| "partial_fill" \| "connection_failure"; count?: number }`. On the paper broker every mode applies. On the 021 account only `accept_then_drop` (the gateway drops 021's answer after 021 accepted: reconciled, nothing sent twice), `connection_failure` (the next order fails in the gateway before any byte is sent: `NOT_PLACED`) and `none` apply, for at most 3 orders, and only when the gateway runs with `CS_021_TEST_FAULTS=true` (`403 FAULTS_DISABLED` otherwise, `422 FAULT_NOT_ON_021` for other modes). Neither changes what 021 does. |
| GET | `/api/demo/state` | `{ feed_mode: "replay" \| "live"; broker_fault: string; broker: "paper" \| "021"; faults_available?: string[] /* 021 */; scenarios: string[] }` |
