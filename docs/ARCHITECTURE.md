# Architecture

## Components
| Module | Path | Notes |
|---|---|---|
| Typed schemas | `backend/countersign/core/schemas.py` | untrusted intents vs canonical (signable) order / plan / mandate / approval payload |
| Canonicalization | `core/canonical.py` | RFC 8785 JSON + SHA-256 |
| Parser (misread guard + offline engine) | `core/parser.py` | extracts what the trader actually stated; routes queries; asks instead of guessing |
| Instrument master | `core/instruments.py` | stable ids, aliases, look-alike (homoglyph) detection, quarantined vendor text, NIFTY expiries (Tuesday, holiday shift) |
| Builder + verifier | `core/verifier.py` | field provenance; lot, tick, freeze, price band, margin, holdings, status, segment, session, freshness, plan consistency, mandate scope; wallet-style preview |
| Proposal service | `core/proposals.py` | the only path from intent to something approvable |
| Accounts and sign-in | `api/main.py`, `auth/factors.py`, `auth/sessions.py` | sign-up with paper account provisioning, password sign-in (Argon2id, lockout), discoverable passkey sign-in, sessions, Origin checks, PIN choice and recovery |
| Approval service | `auth/countersign.py` | challenges, WebAuthn/PIN/TOTP verification, capability minting |
| Capabilities | `core/capabilities.py` | mint + atomic consume |
| Executor | `executor/` | DBOS workflows, outbox, unique tags, reconciliation, tracker (fills, partials, rejections) |
| Mandate engine | `core/mandate_engine.py` | tick-driven, CAS one-shot, NEEDS_REVIEW after downtime |
| Broker interface | `core/broker_client.py` | `ReadGateway` (read key, one cached gateway per account, every call names its account) and `OrderExecutionProvider` (write key); `BrokerAdmin` creates and resets paper accounts; a 021 adapter implements the same interface |
| Paper broker | `broker/app.py` | separate process + DB, account kinds (trader, lab, naive), key scopes, fills from ticks, partial fills, fault injection, per-account create and reset |
| Feed | `feed/main.py` | deterministic replay, scenarios (dip, spike, crash, stale), Valkey fan-out |
| Monitor | `monitor/main.py` | independent invariant checks, kill switch |
| AI layer | `llm/` | Pydantic AI agent (Gemini/Groq/Ollama), tool allowlist, deterministic router, chat SSE |
| Attack Lab | `attack_lab/` | nine scenarios against the real pipeline + naive baseline |
| API | `api/main.py` | FastAPI; contract in `docs/API.md`; every read and write is scoped to the session's user and account |
| Web app | `web/` | Next.js 16 App Router; routes below |

## Web routes
| Route group | Routes | Notes |
|---|---|---|
| `(marketing)` | `/` | Public landing page. Reads only `GET /api/public/status` (no auth) and, for the buttons, whether a session exists. **Explore the Countersign Copilot** goes to `/signin?next=/copilot` when signed out and straight to `/copilot` when signed in. |
| `(auth)` | `/signin`, `/signup`, `/onboarding` | Email and password or passkey sign-in, sign-up, then onboarding: choose the app PIN (required), register a passkey (recommended), link an authenticator app (optional). `?next=` accepts same-origin relative paths only. |
| `(app)` | `/copilot`, `/portfolio`, `/market`, `/outcomes`, `/mandates`, `/plans`, `/attack-lab`, `/security` | The signed-in app inside `AppShell`: one-row header (brand, search, one system status pill, account menu), nav rail or mobile tab bar, kill switch banner only while engaged. |

Guarding happens in two places. `web/src/proxy.ts` (Next 16's renamed middleware) sends any app route without a `cs_session` cookie to `/signin?next=<path>` before rendering. `AppShell` then checks `/api/me`: a `401` goes to sign-in and an account without a PIN (`onboarding.complete === false`) goes to `/onboarding`. The API remains the authority on every request.

## Account provisioning
`POST /api/auth/signup` validates the input, creates the user (email unique case-insensitively, Argon2id password hash, random WebAuthn user handle) and allocates a paper account id `PT` + 6 base32 characters. The API asks the paper broker to create it (`POST /admin/accounts` with the demo admin key), which seeds it exactly like the DEMO1 template: the same cash, seven holdings and one intraday position. The `accounts` row links it to the user, `ACCOUNT_CREATED` is audited and a fresh session is returned. The broker's read and write keys cover every `trader` account and the LAB1 Attack Lab sandbox (checked against `b_accounts` on each request, so provisioned accounts survive restarts), never the `naive` sandbox. `POST /api/demo/reset` calls `POST /admin/accounts/{id}/reset` for the caller's account only.

## Data model (PostgreSQL 18)
`users` (email unique case-insensitively, Argon2id password hash, login failure count and lock), `device_credentials` (public keys only), `webauthn_challenges` (`user_id` empty for passkey sign-in challenges), `sessions`, `accounts` (one paper account per user), `instruments`, `market_snapshots`, `proposals` (canonical JSON + hash + field provenance + issues), `approvals` (payload, payload hash, unique nonce, assertion or server attestation; one VERIFIED per proposal via partial unique index), `capabilities` (CHECK used_at consistent with status), `plans`, `plan_steps`, `mandates` (unique trigger_event_id; CHECK scope), `exec_jobs`, `outbox` (UNIQUE client_order_id, UNIQUE idempotency_key, UNIQUE (capability, step)), `broker_orders`, `order_fills`, `audit_events` (hash chain, append-only trigger), `security_events`, `outcomes`, `system_state` (kill switch), `chat_messages`, `attack_runs`. The paper broker has its own database (`paperbroker`), like a real external broker; `b_accounts.kind` marks trader, lab (LAB1) and naive (NAIVE1) accounts. Schema changes are applied on startup by idempotent, information_schema-guarded migrations (`core/models.py` `MIGRATIONS`, and the broker's own startup), so an existing database upgrades in place.

## Order lifecycle
`USER_REQUEST -> LLM_PARSED / TYPED_INTENT -> instrument resolution -> VERIFIED -> CANONICAL_CREATED -> AWAITING_APPROVAL -> AUTH_CHALLENGE_ISSUED -> SIGNATURE_VERIFIED -> CAPABILITY_MINTED -> EXECUTION_REQUESTED -> EXECUTION_STARTED (re-verified, capability consumed) -> OUTBOX_PREPARED -> BROKER_REQUEST -> BROKER_RESPONSE (ACKED | UNKNOWN) -> RECONCILIATION (FOUND | ABSENT -> one safe RETRY) -> FILL / PARTIAL_FILL / REJECTION / ORDER_CANCELLED`
Every arrow is an audit event. There is no edge from LLM output to the broker.

## Standing mandate lifecycle
`PENDING_APPROVAL -> (countersign; capability expires with the mandate) ACTIVE -> TRIGGERED (CAS, unique trigger_event_id) -> revalidation (holdings, margin, session, fresh tick) -> EXECUTING (executor checks the fired order equals the signed template and is within max quantity / price limit) -> FIRED -> COMPLETED`
Side exits: `NEEDS_REVIEW` (condition became true while offline; never fires stale), `EXPIRED`, `CANCELLED` (capability revoked), `FAILED` (revalidation or broker rejection). Alerts complete on trigger and never place orders.

## Multi-step plan lifecycle
Plan: `AWAITING_APPROVAL -> APPROVED (one signature over the whole canonical plan) -> EXECUTING -> COMPLETED | PARTIALLY_COMPLETED | PARTIALLY_FAILED | REJECTED`.
Step: `PENDING -> SUBMITTED -> OPEN -> PARTIALLY_FILLED -> FILLED`, or `REJECTED | CANCELLED | FAILED | SKIPPED`.
Steps run in order; a step that depends on an earlier one (for example a buy that uses sell proceeds) waits for it to fill. On any failure the remaining steps are SKIPPED with the reason. No compensating trades are ever invented.

## Market data and events
The feed publishes ticks to Valkey (`cs:ticks`, latest tick per instrument, 15-minute history). The broker fills from ticks and publishes order updates (`cs:broker.updates`) that the executor's tracker applies; a periodic sync reconciles missed postbacks. The API relays ticks, outcomes, proposal/mandate updates and monitor status to the browser over SSE (`/api/events`), filtered per user: proposal, mandate, order, plan and outcome events reach only their owner. Valkey is never on a safety-critical path: every safety decision re-reads Postgres or the broker.
