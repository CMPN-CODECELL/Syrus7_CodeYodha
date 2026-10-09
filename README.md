# Countersign

**An AI trading copilot that cannot trade without your signature.**
SYRUS 7.0 | FinTech PS-04 (AI Trading Copilot) | Team CodeYodha

> THE AI CAN PROPOSE. THE SYSTEM CAN VERIFY. THE HUMAN MUST COUNTERSIGN. ONLY THEN CAN THE EXECUTOR ACT.
> Under every attack in the Attack Lab: **UNAPPROVED TRADES = 0**.

Countersign lets a trader query and act on their 021 trading account in plain English (in this build, a paper trading account on a simulated market). The language model is treated as an **untrusted translator, never an actor**: it can read data and draft proposals, but it holds no credentials and has no tool that can place, modify, cancel or approve anything. Every action becomes a canonical, deterministically verified object that the trader countersigns with a device passkey. A separate executor process, the only holder of the broker write key, re-verifies everything and executes each order effectively once.

---

## 100% Free Local Development

Everything runs locally with free and open-source software. No credit card, no billing account, no paid API, no paid hosting.

### Prerequisites (all free)
| Tool | Version used | Why |
|---|---|---|
| Docker Desktop | 28.x | PostgreSQL 18 + Valkey 9 (and optional Toxiproxy) |
| Python + uv | 3.12, uv 0.11+ | backend, executor, broker, feed, monitor |
| Node.js + pnpm | 22.x, pnpm 10 | Next.js web app |
| Ollama | 0.40+ | local LLM (`qwen3:4b-instruct-2507-q4_K_M`, about 2.5 GB, runs on a 6 GB GPU) |
| Java (optional) | 21 | TLA+ model checking (TLC) |

### Start everything
```bash
# 1. one-time setup
cd backend && uv sync && cd ..
cd web && pnpm install && cd ..
ollama pull qwen3:4b-instruct-2507-q4_K_M
cp .env.example .env            # optional: add a free Gemini or Groq key

# 2. run the whole stack (Postgres, Valkey, broker, feed, executor, api, monitor, web)
uv run --project backend python scripts/dev.py

# 3. open http://localhost:3000 (the landing page) -> "Explore the Countersign Copilot" -> create an account
#    stop with Ctrl+C, or: uv run --project backend python scripts/stop.py
```

### First visit
1. **Landing page** at `http://localhost:3000`: what Countersign does, live system status from the running stack, and **Explore the Countersign Copilot**. Use `localhost`, not `127.0.0.1`: passkeys only work on `localhost`.
2. **Sign up** with your name, email and a password (8+ characters). You get your own paper trading account (an id like `PTZWMTPI`), seeded with a sample portfolio on the simulated market. No real money moves.
3. **Onboarding**: choose your own 4-digit app PIN (easy ones such as 1234 or 1111 are refused), register this computer's passkey (Windows Hello or Touch ID), and optionally link an authenticator app. Later, adding a passkey or replacing the authenticator first asks you to confirm with a method you already have: a password or a signed-in session alone can never add a way to countersign.
4. **Copilot**: ask about your portfolio or describe a trade. The AI drafts; you countersign the exact order with your passkey (or PIN or authenticator code as fallbacks).

Next time, sign in with your email and password or with your passkey. Signing in is not approval: every trade still needs a fresh countersignature. Forgot your PIN? Reset it in the Security Center with your account password.
Step-by-step setup for a new machine (tool installs for Windows, macOS and Linux, plus troubleshooting) is in [SETUP.md](SETUP.md).

`scripts/dev.py` generates broker keys into `.secrets/` and hands each process **only** the credential it may hold (see "Credential zones" below). Logs go to `.logs/*.log`.

### Optional free-tier models
Countersign is model-agnostic. With `CS_LLM_PROVIDER=auto` it builds a fallback chain in `CS_LLM_ORDER` order (default `groq,gemini,ollama`), skipping any provider without a key or a running server: Groq `openai/gpt-oss-20b` (if `CS_GROQ_API_KEY` is set), Gemini `gemini-3.5-flash` (if `CS_GEMINI_API_KEY` is set), then local Ollama. If every model fails, the deterministic interpreter answers. In auto mode, read-only questions the deterministic parser recognises with certainty (P&L, losers, average price, option chain) skip the model entirely; anything that drafts an action always goes through the chain. Safety does not change with the model; the chat simply gets faster or smarter. Note: Google's free tier may use prompts to improve its products, so the demo uses only simulated accounts.

All three providers are free: Groq and Gemini free-tier keys, or Ollama fully offline. No paid API is used anywhere.

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
  │                                   band; outbox + unique tag;        │
  │                                   reconcile before retry; DBOS      │
  └────────┬─────────────────────────────────┬──────────────────────────┘
           ▼                                 ▼
     021 broker adapter / fault-injecting paper broker      feed + Valkey bus
  PROOF: hash-chained audit log, independent invariant monitor, Attack Lab, Hypothesis, TLA+
```

| Process | Port | Holds | Role |
|---|---|---|---|
| `api` | 8000 | broker READ key (+ demo admin key, sandbox-only naive key) | accounts and sign-in, chat, per-user read gateway, verifier, approval service, mandates engine, audit, Attack Lab, SSE. **Refuses to start if given the write key.** |
| `executor` | - | broker WRITE key only | the only process that can place/modify/cancel; never imports AI code (checked at startup) |
| `broker` | 8100 | issues/validates key scopes | fault-injecting paper broker with its own database (stands in for 021); one paper account per trader |
| `feed` | - | nothing | deterministic tick replay (stands in for the 021 WebSocket) |
| `monitor` | - | AUDIT (read-all) key | independent invariant monitor; trips the kill switch |
| `web` | 3000 | nothing (cookies only) | Next.js 16 app: landing page, sign-in, sign-up, onboarding and the signed-in app |

Stack: Next.js 16, React 19, TypeScript, Tailwind CSS 4, shadcn/ui, AI Elements, Lightweight Charts, Motion | Python 3.12, FastAPI, Pydantic AI (Gemini, Groq, Ollama), DBOS, PostgreSQL 18, Valkey 9 | WebAuthn (py_webauthn + SimpleWebAuthn), RFC 8785 | Hypothesis, Toxiproxy, TLA+.

---

## Documentation
| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data model, order / mandate / plan lifecycles |
| [docs/SECURITY.md](docs/SECURITY.md) | security model, threat model, authentication flow, invariants |
| [docs/ATTACK_LAB.md](docs/ATTACK_LAB.md) | the nine attack scenarios and their results |
| [docs/TESTING.md](docs/TESTING.md) | test strategy and results (pytest + Hypothesis, TLA+/TLC, Attack Lab) |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | the judge demo, step by step |
| [docs/PS_MAPPING.md](docs/PS_MAPPING.md) | PS-04 requirement -> implementation -> demo evidence |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | known limitations and trade-offs |
| [docs/API.md](docs/API.md) | HTTP + SSE contract between web and api |
| [SETUP.md](SETUP.md) | step-by-step setup on a new machine, troubleshooting |

## Key commands
| Command | What it does |
|---|---|
| `uv run --project backend python scripts/dev.py` | start the full stack |
| `uv run --project backend python scripts/stop.py` | stop it |
| `cd backend && uv run pytest -q` | 72 tests incl. Hypothesis stateful test, accounts and cross-user isolation (needs Postgres + Valkey up) |
| `cd formal && java -cp tla2tools.jar tlc2.TLC -config Countersign.cfg -deadlock Countersign.tla` | model-check the protocol |
| `uv run --project backend python scripts/smoke.py` | end-to-end smoke test over HTTP (automation demo account) |
| `uv run --project backend python scripts/flows.py` | standing instruction, partial-fill plan, modify and cancel over HTTP |
| `python scripts/e2e_browser.py [--device "Pixel 7"]` | full browser journey against the running stack: landing, sign up, onboarding with your own PIN, passkey and PIN countersign, Attack Lab, kill switch, password and passkey sign-in, cross-user isolation (needs Python Playwright) |
