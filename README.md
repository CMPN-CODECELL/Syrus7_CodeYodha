# Countersign

**An AI trading copilot that cannot trade without your signature.**
SYRUS 7.0 | 021-01 "Copilot, Not Autopilot" (AI Trading Copilot, our PS-04) | Team CodeYodha

> THE AI CAN PROPOSE. THE SYSTEM CAN VERIFY. THE HUMAN MUST COUNTERSIGN. ONLY THEN CAN THE EXECUTOR ACT.
> Under every attack in the Attack Lab: **UNAPPROVED TRADES EXECUTED BY COUNTERSIGN = 0** (scenario I's out-of-band order, placed straight at the broker with stolen keys, is detected by the independent monitor, which engages the kill switch).

Countersign lets a trader query and act on their 021 trading account in plain English. It trades on the real **021 Developer Sandbox** through a dedicated `gateway021` process, and also runs a local fault-injecting paper broker (one paper account per user, the Attack Lab sandboxes and offline demos). The language model is treated as an **untrusted translator, never an actor**: it can read data and draft proposals, but it holds no credentials and has no tool that can place, modify, cancel or approve anything. Every action becomes a canonical, deterministically verified object that the trader countersigns (passkey, or app PIN / authenticator code as lower-assurance fallbacks). A separate executor process, the only holder of the broker write key, re-verifies everything and sends each order at most once to 021 (it never retries an order on 021 automatically).

Countersign is a hackathon prototype: it trades only on the 021 Developer Sandbox (virtual funds) and on local paper accounts. It is not built, audited or approved for real-money trading (see docs/LIMITATIONS.md, item 14).

---

## 100% Free Local Development

Everything runs locally with free and open-source software. No credit card, no billing account, no paid API, no paid hosting. The 021 sandbox is free and is used only when you put sandbox credentials in `.env`.

### Prerequisites (all free)
| Tool | Version used | Why |
|---|---|---|
| Docker Desktop | 28.x | PostgreSQL 18 + Valkey 9 (and optional Toxiproxy) |
| Python + uv | 3.12, uv 0.11+ | backend, executor, brokers, feed, monitor |
| Node.js + pnpm | 22.x, pnpm 10 | Next.js web app |
| Ollama | 0.40+ | local LLM (`qwen3:4b-instruct-2507-q4_K_M`, about 2.5 GB, runs on a 6 GB GPU) |
| Java (optional) | 21 | TLA+ model checking (TLC) |

### Start everything
```bash
# 1. one-time setup
cd backend && uv sync && cd ..
cd web && pnpm install && cd ..
ollama pull qwen3:4b-instruct-2507-q4_K_M
cp .env.example .env            # optional: a free Gemini or Groq key, and the 021 sandbox login (see below)

# 2. run the whole stack (Postgres, Valkey, paper broker, gateway021 if configured, feed, executor, api, monitor, web)
uv run --project backend python scripts/dev.py

# 3. open http://localhost:3000 (the landing page) -> "Explore the Countersign Copilot" -> create an account
#    stop with Ctrl+C, or: uv run --project backend python scripts/stop.py
```
On Windows every child process starts without a console window.

### First visit
1. **Landing page** at `http://localhost:3000`: what Countersign does, live system status from the running stack, and **Explore the Countersign Copilot**. Passkeys work on `localhost` and on any HTTPS origin listed in `CS_EXTRA_ORIGINS` (the WebAuthn RP ID is the page's hostname, so a passkey belongs to the host it was registered on); on `127.0.0.1` use the PIN.
2. **Sign up** with your name, email and a password (8+ characters). You get your own paper trading account (an id like `PTZWMTPI`), seeded with a sample portfolio. Users listed in `CS_021_OWNER_EMAILS` are also linked to the real 021 sandbox account and land on it.
3. **Onboarding**: choose your own 4-digit app PIN (easy ones such as 1234 or 1111 are refused), register this computer's passkey (Windows Hello or Touch ID), and optionally link an authenticator app. Later, adding a passkey or replacing the authenticator first asks you to confirm with a method you already have: a password or a signed-in session alone can never add a way to countersign.
4. **Copilot**: ask about your portfolio or describe a trade. The AI drafts; you countersign the exact order.

Next time, sign in with your email and password or with your passkey. Signing in is not approval: every trade still needs a fresh countersignature. Forgot your PIN? Reset it in the Security Center with your account password.
Step-by-step setup for a new machine (tool installs for Windows, macOS and Linux, the 021 sandbox, troubleshooting) is in [SETUP.md](SETUP.md).

`scripts/dev.py` generates broker keys into `.secrets/` and hands each process **only** the credential it may hold (see "Credential zones" below). Logs go to `.logs/*.log`.

### Optional free-tier models
Countersign is model-agnostic. With `CS_LLM_PROVIDER=auto` it builds a fallback chain in `CS_LLM_ORDER` order (default `groq,gemini,ollama`), skipping any provider without a key or a running server: Groq `openai/gpt-oss-20b` (if `CS_GROQ_API_KEY` is set), Gemini `gemini-3.5-flash` (if `CS_GEMINI_API_KEY` is set), then local Ollama. If every model fails, the deterministic interpreter answers. In auto mode, read-only questions the deterministic parser recognises with certainty (P&L, losers, average price, option chain) skip the model entirely; anything that drafts an action always goes through the chain. Safety does not change with the model; the chat simply gets faster or smarter. Note: Google's free tier may use prompts to improve its products.

---

## The 021 Developer Sandbox

With `CS_021_UCC` and `CS_021_PASSWORD` in `.env`, `scripts/dev.py` starts **`gateway021`** (port 8200). It is the **only** process that loads those two values or ever holds the 021 password and access token (the UCC, the 021 login code, is also used as the 021 account's id in Countersign's database and API responses, shown masked in the UI; see `docs/LIMITATIONS.md`); `dev.py` strips them from every other process, and the API and executor refuse to start if the 021 password is in their environment.

- **Same interface as the paper broker.** The gateway serves the paper broker's `/v1` API with the same key scopes (read, write, admin, audit; the naive key is refused), so the read gateway, the executor and the monitor reach 021 by base URL alone. It serves exactly one 021 account.
- **Linking (hackathon model).** Users whose sign-in email is listed in `CS_021_OWNER_EMAILS` are linked to that 021 account on sign-up or sign-in and land on it; they can switch to their paper account from the account menu or the Security Center (Broker connection). Everyone else only ever sees their own paper account. Self-service linking of any 021 account is future scope.
- **One live token per account.** A new 021 login revokes the previous token and there is no refresh token, so the gateway logs in once (single-flight), caches the token until `expiresAt` and re-logs in exactly once on a 401, then fails closed. Do not run a second client (or the probe script) against the same account while the stack runs: it would revoke the gateway's token.
- **Orders without client tags.** 021 has no client order tag, so the gateway keeps its own write-ahead record per placement (tag, request fingerprint, send time) and sends `POST /orders` exactly once. A lost or ambiguous answer (timeout, dropped connection, 429, 500, 503) is reconciled against `GET /orders`: exactly one matching order placed after the send time is mapped; none is `NOT_PLACED` (or `REJECTED` when 021 refused it); more than one is `AMBIGUOUS` and is never guessed; an unreadable order book is `UNCONFIRMED`. An identical order is refused while one is unresolved (`409 DUPLICATE_IN_FLIGHT`).
- **No automatic retry on 021.** The executor never resends a placement, modification or cancellation to 021. `NOT_PLACED` tells the trader that nothing was sent twice and that a fresh countersignature is needed to resend; `AMBIGUOUS` and `UNCONFIRMED` stop as "needs your review". Reads (GET) are retried with backoff and jitter on 429, 5xx and timeouts.
- **REST is the source of truth.** The orders websocket only says which order changed; the gateway then reads that order (and its trades) over REST, catches up fully on every reconnect and also polls the order book every 3 seconds. No 021 status is treated as final: an order 021 confirmed as cancelled that later turns executed is reported as "Filled after its cancellation". Market orders often fill in many small trades: the filled quantity is 021's traded quantity and the average price is the quantity-weighted average of the order's trades (`GET /orders/{id}/trades`); trade times are corrected for the shift the sandbox applies.
- **Live market data.** The market websocket (full mode, at most 95 instruments per connection) is decoded into the same Valkey ticks the replay feed writes. The sandbox interleaves two price series for some instruments, so the gateway publishes the median of the last 5 updates; it reconnects with backoff and a fresh ephemeral key, and stops refreshing ticks when the connection is not provably alive, so stale prices block trading.
- **Funds are an estimate.** 021 has no funds API, so available funds are shown as an estimate (Rs 10,00,000 virtual daily funds minus open buy orders and today's net buys); 021's own risk checks decide.
- **Orders placed outside Countersign** (021 web app, scripts) are recorded by the monitor as informational events, never as violations, unless `CS_021_STRICT_MONITOR=true`.
- **Fault drill.** With `CS_021_TEST_FAULTS=true` (off by default), the presenter controls on the 021 account can drill two faults inside the gateway, never at 021: "Connection fail" (the next order fails before any byte is sent: `NOT_PLACED`) and "Accept, drop ack" (021 accepts the order and the gateway drops the answer: reconciliation finds it, nothing is sent twice).
- **Simulated prices never reach 021.** Presenter feed scenarios (spike, crash) can drive a live instrument for up to two minutes for paper demos; while they do, orders and standing instructions on the 021 account are blocked or paused for that instrument.

## What the copilot does (021-01 levels)

| Level | Examples that work | How |
|---|---|---|
| L1 read-only | "What's my P&L today?", "Which positions are down more than 5%?", "What did I pay on average for INFY?", "Show NIFTY options near the money for this expiry" | Read tools over the read key only; on 021 the option chain comes from the live 021 chain or the answer says it cannot (no simulated prices) |
| L2 orders | "Buy 10 Reliance at market", "Sell half my TCS", "Move my stop-loss on HDFC Bank up to 690" (with a sell stop-loss already resting on HDFC Bank; the official text uses 1640, which at sandbox prices near Rs 700 is refused because a sell trigger must be below the last price), "Cancel my ITC order", SL and SL-M | Exact canonical order with a source for every field; market orders carry a signed protection price (sent to 021 as a limit at that price); fractions computed from what you can sell, rounded down to the lot; stop-loss moves send the full new order state; tick size and price band are checked and never rounded silently |
| L3 standing instructions | "If Reliance drops below 1150, buy 10", "Tell me when any holding falls 3% in a day", price alerts | Durable in Postgres, armed only after a countersignature, fire once (compare-and-set), survive restarts, go to review instead of firing stale after downtime. A condition that is already true when drafted is never armed silently: the copilot states the live price and offers an order now or a trigger on the other side of the price |
| L4 plans | "Exit all my losing intraday positions", "Rebalance so no single stock is more than 20% of my portfolio" | One plan, one signature over every step, the arithmetic shown on the card, each step's fill, partial fill or rejection reported; no compensating trades |

A rock-solid L2 comes first: every order, on paper or 021, goes through the same verifier, countersignature, single-use capability and executor re-verification.

---

## What is inside

```
                    TRADER (browser, Next.js)
      web chat ── approval card (passkey) ── live outcomes
          │ ask           ▲ preview      │ signature        ▲ fills
          ▼               │              ▼                  │
  ┌─ UNTRUSTED ─────┐  ┌─ CORE (deterministic) ─────────────┴──────────┐
  │ LLM agent       │  │ verifier (lot, tick, freeze, band, margin...) │
  │ read tools +    │─▶│ approval service (WebAuthn / PIN / TOTP)      │
  │ draft tools only│  │ durable mandates engine (CAS, once)           │
  │ no credentials  │  └──────────────┬──────────────────┬─────────────┘
  └────────┬────────┘                 │ single-use        │ fires once
           │ read                     ▼ capability        ▼
  ┌─ CREDENTIALS ───────────────────────────────────────────────────────┐
  │ read-only gateway (READ key)      executor (WRITE key, no AI code)  │
  │                                   re-verify hash, signature, expiry,│
  │                                   band; outbox; reconcile, never    │
  │                                   blind retry; DBOS                 │
  └────────┬─────────────────────────────────┬──────────────────────────┘
           ▼                                 ▼
   gateway021 (021 UCC, password, token)  ->  021 Developer Sandbox (REST + websockets)
   fault-injecting paper broker (paper accounts, Attack Lab)     feed + Valkey bus
  PROOF: hash-chained audit log, independent invariant monitor, Attack Lab, Hypothesis, TLA+
```

| Process | Port | Holds | Role |
|---|---|---|---|
| `api` | 8000 | broker READ key (+ demo admin key, sandbox-only naive key) | accounts and sign-in, chat, per-user read gateway, verifier, approval service, mandates engine, audit, Attack Lab, SSE. **Refuses to start if given the write key or the 021 password.** |
| `executor` | - | broker WRITE key only | the only process that can place/modify/cancel; never imports AI code (checked at startup); routes each order to the paper broker or `gateway021` by account |
| `gateway021` | 8200 | the 021 UCC (login), password and access token (+ the broker keys it validates); the UCC also serves as the 021 account id elsewhere | only when configured: the 021 sandbox behind the paper broker's `/v1` interface; reconciliation, live market and order websockets, instrument file |
| `broker` | 8100 | issues/validates key scopes | fault-injecting paper broker with its own database; one paper account per trader, plus the LAB1 and NAIVE1 Attack Lab sandboxes |
| `feed` | - | nothing | deterministic replay; with the gateway on it replays only the fictional instruments (ABCCORP, XYZSUSP, TATAMOTORS, the look-alike RELIANCE) and drives presenter scenarios |
| `monitor` | - | AUDIT (read-all) key | independent invariant monitor over the paper broker and the 021 order book; trips the kill switch |
| `web` | 3000 | nothing (cookies only) | Next.js 16 app: landing page, sign-in, sign-up, onboarding and the signed-in app |

Stack: Next.js 16, React 19, TypeScript, Tailwind CSS 4, shadcn/ui, AI Elements, Lightweight Charts, Motion | Python 3.12, FastAPI, Pydantic AI (Gemini, Groq, Ollama), DBOS, PostgreSQL 18, Valkey 9, httpx, websockets | WebAuthn (py_webauthn + SimpleWebAuthn), RFC 8785 | Hypothesis, Toxiproxy, TLA+.

---

## Documentation
| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, the gateway021 process, data model, order / mandate / plan lifecycles |
| [docs/SECURITY.md](docs/SECURITY.md) | security model, credential zones, threat model, authentication flow, invariants |
| [docs/ATTACK_LAB.md](docs/ATTACK_LAB.md) | the nine attack scenarios and their results |
| [docs/TESTING.md](docs/TESTING.md) | test strategy and results (pytest + Hypothesis, TLA+/TLC, Attack Lab) |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | the judge demo, step by step |
| [docs/PS_MAPPING.md](docs/PS_MAPPING.md) | problem statement requirement -> implementation -> demo evidence |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | known limitations and trade-offs |
| [docs/API.md](docs/API.md) | HTTP + SSE contract between web and api |
| [SETUP.md](SETUP.md) | step-by-step setup on a new machine, the 021 sandbox, troubleshooting |

## Key commands
| Command | What it does |
|---|---|
| `uv run --project backend python scripts/dev.py` | start the full stack (add `--no-021` to run without the 021 gateway even when it is configured) |
| `uv run --project backend python scripts/stop.py` | stop it |
| `cd backend && uv run pytest -q` | the backend test suite (292 tests, all passing on 2026-10-09) incl. a Hypothesis stateful test, accounts, cross-user isolation and the 021 gateway against recorded fixtures (needs Postgres + Valkey up; run one suite at a time) |
| `cd formal && java -cp tla2tools.jar tlc2.TLC -config Countersign.cfg -deadlock Countersign.tla` | model-check the protocol |
| `uv run --project backend python scripts/smoke.py` | end-to-end smoke test over HTTP (automation demo account) |
| `uv run --project backend python scripts/flows.py` | standing instruction, partial-fill plan, modify and cancel over HTTP |
| `uv run --project backend python scripts/smoke_021.py --e2e <owner email> --e2e-only` | live check of the 021 path through the running stack (places and cancels a small order on the 021 account) |
| `python scripts/e2e_browser.py [--device "Pixel 7"] [--base URL]` | full browser journey on a new paper account: landing, sign up, onboarding with your own PIN, passkey and PIN countersign, Attack Lab, kill switch, password and passkey sign-in, cross-user isolation (needs Python Playwright) |
| `python scripts/e2e_browser.py --broker 021 --login-file <file>` | browser journey as a linked 021 owner: L1 questions on the live account, the official standing-instruction example, and a countersigned order and cancellation on 021 |

## Future scope
- **Self-service 021 linking**: today the operator links 021 accounts by email (`CS_021_OWNER_EMAILS`) and one gateway serves one 021 account; per-user linking (each trader entering their own UCC, one gateway session per account) is not built.
- **Funds from 021**: the sandbox has no funds or margin endpoint, so funds are an estimate.
- **Plans that continue past a refused step**: a plan stops sending at the first step the broker refuses when it is sent (later steps are skipped and reported); a per-plan "continue on failure" policy is not built.
- **Rebalancing with buys and with positions**: rebalance sells holdings only.
- **Standing orders across all holdings**: only alerts can watch every holding.
- **BSE and F&O order placement on 021**: the instrument working set is NSE stocks, NIFTY / BANKNIFTY and NIFTY options for display; orders were exercised on NSE cash only.
- **Operator roles**: the kill switch, presenter controls and Attack Lab are shared by every account on one installation.
