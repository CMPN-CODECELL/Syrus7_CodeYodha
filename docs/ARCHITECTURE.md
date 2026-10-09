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
| Broker interface | `core/broker_client.py` | `ReadGateway` (read key, one cached gateway per account, every call names its account) and `OrderExecutionProvider` (write key); `BrokerAdmin` creates and resets paper accounts. Each account has a `broker_kind` (`paper` or `021`) that picks the base URL: the paper broker or `gateway021` |
| 021 gateway | `broker021/` | separate process (`gateway021`, port 8200), the only holder of the 021 UCC, password and token: REST client (`client.py`), the paper broker's `/v1` semantics over 021 with write-ahead placement records and reconciliation (`gateway.py`, `store.py`), exact request mapping (`mapping.py`), binary websocket decoding (`wire.py`), live market ticks (`marketdata.py`), order events (`orders_ws.py`), the daily instrument file (`instruments021.py`) and the owner-email linking model (`linking.py`, used by the API; holds no credential) |
| Paper broker | `broker/app.py` | separate process + DB, account kinds (trader, lab, naive), key scopes, fills from ticks, partial fills, fault injection, per-account create and reset; with the gateway on, paper seed averages are rescaled to live 021 closes when an account is created or reset |
| Feed | `feed/main.py` | deterministic replay, scenarios (dip, spike, crash, stale), Valkey fan-out; with the gateway on it replays only the fictional instruments, and a scenario may drive a 021-priced instrument for at most two minutes (never used for the 021 account's orders or standing instructions) |
| Monitor | `monitor/main.py` | independent invariant checks over the paper broker and the 021 order book (audit key), kill switch; orders on the 021 account that Countersign did not place are informational |
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

## The 021 gateway (zones and flow)
```
 api (READ key) ──┐                               ┌── REST https://devapi.021.trade/api/developer-api/v1
 executor (WRITE) ├─ /v1 + X-Account ─▶ gateway021 ┤   (token: single-flight login, cached to expiresAt)
 monitor (AUDIT) ─┘   same key scopes    (UCC,     ├── market websocket -> Valkey ticks (median of 5)
                      as the paper       password, └── orders websocket -> GET /orders/{id} -> cs:broker.updates
                      broker             token)
```
- **Credential zone.** Only `gateway021` reads `CS_021_UCC` and `CS_021_PASSWORD` (its own settings class, `broker021/settings.py`) and holds the access token in memory. `scripts/dev.py` removes both variables from every other process; the API and the executor exit at startup if the 021 password is in their environment. The API learns the 021 account id (the UCC) from the gateway's `GET /v1/account` with its read key, and shows it masked.
- **Placement.** `POST /v1/orders` maps the signed order exactly (`MARKET` becomes a limit at the signed protection price; `SL-M` an SL at the protection price with the trigger), refuses a tag already used and an identical order while one is unresolved, writes the attempt row, then sends `POST /orders` once. A lost answer is reconciled against `GET /orders` by fingerprint and send time (exactly one match: mapped; none: `NOT_PLACED`; several: `AMBIGUOUS`; order book unreadable: `UNCONFIRMED`). `GET /v1/attempts/{tag}` lets the executor ask for a verdict instead of resending. Attempts left `SENDING` by a gateway crash are reconciled at startup, and an order that shows up within 15 minutes of a `NOT_PLACED` verdict given after a lost answer is mapped late and reported.
- **Modify and cancel.** `PUT /orders/{id}` always carries the full new order state; cancellations are asynchronous at 021 (`SentForCancellation`, then `Cancelled`). Both are confirmed on the target order's real state and never retried.
- **Order state.** REST is the source of truth: order websocket events trigger a fresh `GET /orders/{id}`, every reconnect runs a full catch-up and the book is polled every 3 s. Orders are published in the paper broker's shape (with raw 021 fields such as `status_021`, `price_021_paise` and `origin`), so the executor's tracker and the UI work unchanged. A late fill after a confirmed cancellation is applied and reported.
- **Market data.** Full-mode subscriptions (at most 95 instruments per connection) for the instrument working set, active standing instructions, open 021 orders and instruments the API asks for; ticks go to the same Valkey keys as the replay feed. Unchanged instruments are re-stamped once a second only while the connection is provably alive, so a dead socket makes prices stale and the verifier blocks trading.
- **Instruments.** The daily 021 instrument file is downloaded by the gateway (it needs the token), cached, and loaded into Postgres as credential-free data that the API reads to resolve symbols.

## Account provisioning
`POST /api/auth/signup` validates the input, creates the user (email unique case-insensitively, Argon2id password hash, random WebAuthn user handle) and allocates a paper account id `PT` + 6 base32 characters. The API asks the paper broker to create it (`POST /admin/accounts` with the demo admin key), which seeds it exactly like the DEMO1 template: the same cash, seven holdings and one intraday position. The `accounts` row links it to the user, `ACCOUNT_CREATED` is audited and a fresh session is returned. When the gateway runs, a user whose email is in `CS_021_OWNER_EMAILS` is also linked to the 021 account on sign-up and sign-in (`accounts.broker_kind = '021'`, further users through `account_members`) and it becomes their active account (`users.active_account_id`); the list is authoritative, so removing an email revokes access on the next request. `POST /api/broker/active` switches between a trader's accounts. The broker's read and write keys cover every `trader` account and the LAB1 Attack Lab sandbox (checked against `b_accounts` on each request, so provisioned accounts survive restarts), never the `naive` sandbox. `POST /api/demo/reset` calls `POST /admin/accounts/{id}/reset` for the caller's account only.

## Data model (PostgreSQL 18)
`users` (email unique case-insensitively, Argon2id password hash, login failure count and lock), `device_credentials` (public keys only), `webauthn_challenges` (`user_id` empty for passkey sign-in challenges), `sessions`, `accounts` (one paper account per user), `instruments`, `market_snapshots`, `proposals` (canonical JSON + hash + field provenance + issues), `approvals` (payload, payload hash, unique nonce, assertion or server attestation; one VERIFIED per proposal via partial unique index), `capabilities` (CHECK used_at consistent with status), `plans`, `plan_steps`, `mandates` (unique trigger_event_id; CHECK scope), `exec_jobs`, `outbox` (UNIQUE client_order_id, UNIQUE idempotency_key, UNIQUE (capability, step)), `broker_orders`, `order_fills`, `audit_events` (hash chain, append-only trigger), `security_events`, `outcomes`, `system_state` (kill switch), `chat_messages`, `attack_runs`. The paper broker has its own database (`paperbroker`), like a real external broker; `b_accounts.kind` marks trader, lab (LAB1) and naive (NAIVE1) accounts. Schema changes are applied on startup by idempotent, information_schema-guarded migrations (`core/models.py` `MIGRATIONS`, and the broker's own startup), so an existing database upgrades in place.

## Order lifecycle
`USER_REQUEST -> LLM_PARSED / TYPED_INTENT -> instrument resolution -> VERIFIED -> CANONICAL_CREATED -> AWAITING_APPROVAL -> AUTH_CHALLENGE_ISSUED -> SIGNATURE_VERIFIED -> CAPABILITY_MINTED -> EXECUTION_REQUESTED -> EXECUTION_STARTED (re-verified, capability consumed) -> OUTBOX_PREPARED -> BROKER_REQUEST -> BROKER_RESPONSE (ACKED | UNKNOWN) -> RECONCILIATION (FOUND | ABSENT -> one safe RETRY, paper broker only) -> FILL / PARTIAL_FILL / REJECTION / ORDER_CANCELLED`. On a 021 account nothing is retried: the gateway's verdict is final for that countersignature (`NOT_PLACED`: countersign again to resend; `AMBIGUOUS` or `UNCONFIRMED`: `NEEDS_REVIEW`).
Every arrow is an audit event. There is no edge from LLM output to the broker.

## Standing mandate lifecycle
`PENDING_APPROVAL -> (countersign; capability expires with the mandate) ACTIVE -> TRIGGERED (CAS, unique trigger_event_id) -> revalidation (holdings, margin, session, fresh tick) -> EXECUTING (executor checks the fired order equals the signed template and is within max quantity / price limit) -> FIRED -> COMPLETED`
Side exits: `NEEDS_REVIEW` (condition became true while offline; never fires stale), `EXPIRED`, `CANCELLED` (capability revoked), `FAILED` (revalidation or broker rejection). Alerts complete on trigger and never place orders; an alert over every holding ("any holding falls 3% in a day") stays active and fires at most once per holding per IST trading day (a durable dedupe row per mandate, instrument and day). A price condition that is already true when drafted is not turned into a mandate: the verifier asks instead, offering an order now or a trigger beyond the live price.

## Multi-step plan lifecycle
Plan: `AWAITING_APPROVAL -> APPROVED (one signature over the whole canonical plan) -> EXECUTING -> COMPLETED | PARTIALLY_COMPLETED | PARTIALLY_FAILED | REJECTED`.
Step: `PENDING -> SUBMITTED -> OPEN -> PARTIALLY_FILLED -> FILLED`, or `REJECTED | CANCELLED | FAILED | SKIPPED`.
Steps run in order; a step that depends on an earlier one (for example a buy that uses sell proceeds) waits for it to fill. If a step cannot be sent (the broker refuses it, or on 021 it is not placed or needs review), or a dependency does not fill, the remaining steps are SKIPPED with the reason; a rejection the broker reports after accepting a step does not stop independent steps (exit and rebalance plans have no dependencies). No compensating trades are ever invented.

## Market data and events
With the gateway on, live 021 ticks for real instruments come from `gateway021` and the feed replays only the fictional ones; otherwise the feed publishes every tick. The feed publishes ticks to Valkey (`cs:ticks`, latest tick per instrument, 15-minute history). The broker fills from ticks and publishes order updates (`cs:broker.updates`) that the executor's tracker applies; a periodic sync reconciles missed postbacks. The API relays ticks, outcomes, proposal/mandate updates and monitor status to the browser over SSE (`/api/events`), filtered per user: proposal, mandate, order, plan and outcome events reach only their owner. Valkey is never on a safety-critical path: every safety decision re-reads Postgres or the broker.
