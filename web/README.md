# Countersign web

The trader-facing UI for **Countersign**, an AI trading copilot that cannot trade without your signature.

> The AI can propose. The system can verify. The human must countersign. Only then can the executor act.

Next.js 16 (App Router, Turbopack), React 19, TypeScript (strict), Tailwind CSS 4, shadcn/ui on Base UI primitives,
Vercel AI Elements for the chat surface, Lightweight Charts, TanStack Query, zustand, motion, NumberFlow.
Everything is free and open source. The browser never talks to a broker and never holds a secret.

## Run it

Requirements: Node 22+, pnpm 10+.

```bash
cd web
pnpm install

# 1. Fully offline demo on realistic fixtures (no backend needed)
NEXT_PUBLIC_MOCK=1 pnpm dev            # http://localhost:3000

# 2. Against the FastAPI backend (docs/API.md) on :8000
pnpm dev

# Production build
pnpm build && pnpm start               # add NEXT_PUBLIC_MOCK=1 to BOTH commands for a mock build
pnpm lint
```

On Windows PowerShell use `$env:NEXT_PUBLIC_MOCK="1"; pnpm dev`.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BACKEND_URL` | `http://127.0.0.1:8000` | Where `/api/*` is proxied (server side only). |
| `NEXT_PUBLIC_MOCK` | unset | `1` serves fixtures and simulations in the browser instead of calling the API. Inlined at build time. |

No other configuration exists, and no API keys belong in this app.

## Mock mode

`NEXT_PUBLIC_MOCK=1` swaps the API client for an in-browser simulation behind the same typed interface
(`src/lib/api.ts`). It is a full demo, not static screens:

- Account `DEMO1` with RELIANCE, INFY, TCS, HDFCBANK, TATAMOTORS (down more than 5%), SBIN and ITC; NIFTY near 25,400 with 50-point strikes and lot size 65.
- A live tick feed (random walk with mean reversion) over the same event bus the real SSE uses.
- A deterministic chat parser that streams tool chips, text and cards, and creates real proposals (orders, 2-step plans, mandates, alerts) from the suggested prompts.
- Seeded proposals: one BLOCKED (the AI inferred quantity 1000 where you said 100), one AWAITING_APPROVAL order, one 2-step plan, one ACTIVE mandate, one NEEDS_REVIEW mandate and history.
- The full countersign ceremony (60 s window, payload hash, method errors), executor progression, reconcile-not-retry, mandate firing, kill switch, Attack Lab runs (all BLOCKED, naive copilot unapproved), audit chain verification and presenter controls.
- Demo credentials: app PIN **1234**, authenticator code **123456**. The passkey prompt is simulated in mock mode (a real authenticator cannot sign a challenge a mock server invented).
- State is kept in `sessionStorage` so a refresh does not lose the story. "Reset demo" in Presenter controls restores the seed.

## Demo script (works in both modes)

1. Sign in to the demo account (logging in is not approval).
2. Copilot: "What is my P&L today?", then "Buy 10 RELIANCE at 2800". Inspect the provenance chips and the wallet preview, then **Countersign**: confirm the exact payload hash, authenticate, and watch the execution timeline (signature verified, single-use capability, executor re-verification, unique tag, broker ack, fill).
3. Ctrl K, "Show latest blocked proposal": the AI misread 100 as 1000, the row is flagged and Countersign is disabled with the reason.
4. Presenter controls: Spike RELIANCE while an order awaits approval: "Price left your approved band. This approval is void; review again."
5. Presenter controls: broker fault "Accept, drop ack", then countersign an order: "Reconciled, not retried".
6. Mandates: Dip to trigger on TATAMOTORS: the armed mandate fires once on a fresh tick.
7. Attack Lab: Run all attacks. UNAPPROVED EXECUTIONS stays 0 while the naive copilot is red.
8. Security Center: trust boundary facts, engage the kill switch (instant), disengage with the PIN, verify the audit chain.

## How it talks to the backend

- `next.config.ts` rewrites `/api/:path*` to `BACKEND_URL`, so the HttpOnly `cs_session` cookie is same-origin.
- Every POST, PUT and DELETE sends `X-CSRF-Token` with the value of the readable `cs_csrf` cookie, with `credentials: "include"`. Nothing is stored in localStorage.
- Streaming endpoints go through route handlers that pipe the upstream body without buffering:
  - `POST /api-stream/chat` to `POST /api/chat` (chat SSE: meta, tool, text-delta, card, done, error)
  - `GET /api-stream/events` to `GET /api/events` (one shared EventSource: tick, outcome, proposal, mandate, security, monitor, audit, ping)
- The live connection reconnects with exponential backoff (1 s up to 20 s, jittered) and shows its state in the top bar.
- Approval cards are rendered **only** from `GET /api/proposals/{id}`. Chat text is never rendered as an order.
- WebAuthn uses `@simplewebauthn/browser`: `startAuthentication({ optionsJSON: start.webauthn })` and `startRegistration({ optionsJSON })`.
- Errors show the backend `message` and `code`. Approval codes (`APPROVAL_EXPIRED`, `PRICE_DRIFT`, `STALE_MARKET_DATA`, `SIGNATURE_INVALID`, `PIN_INVALID`, `PIN_LOCKED`, `KILL_SWITCH_ENGAGED`, and so on) get specific guidance and, where it makes sense, a "Start a new 60 s window" action.

## Structure

```
src/
  app/                     routes: / (Copilot), portfolio, market, outcomes, mandates, plans, attack-lab, security
    api-stream/            streaming proxies for chat and live events
  components/
    shell/                 app shell, nav rail, top bar, authority strip, health dots, command palette, presenter controls, first run, live bridge
    chat/                  copilot view, messages and tool chips, composer (AI Elements PromptInput), right pane
    cards/                 chat cards: account, holdings, positions, orders, quote, option chain, clarify, notice, proposal
    approval/              approval card, provenance rows, wallet preview, price band meter, issues, countersign dialog, countdown ring, execution timeline, inline passkey and PIN setup
    plans/ mandates/       plan card and page, mandate card and page
    market/ portfolio/     watchlist, Lightweight Charts price chart, option chain table; KPI strip and tables
    outcomes/              live outcome feed and page
    attack-lab/ security/  Attack Lab and Security Center
    ai-elements/           Vercel AI Elements (Conversation, Message, PromptInput, Shimmer)
    ui/                    shadcn/ui (Base UI) plus custom primitives: chip, kpi, live price, data table, panel, states, hash, sparkline
  hooks/                   useNow (shared clock), useMediaQuery
  lib/
    types.ts               types mirrored from docs/API.md
    api.ts                 one typed client interface; real implementation; mock switch
    errors.ts env.ts       ApiError and approval error copy; NEXT_PUBLIC_MOCK flag
    sse.ts                 SSE parser for fetch streams; reconnecting EventSource
    store.ts               zustand: live ticks/outcomes/monitor, UI requests, chat transport
    queries.ts             TanStack Query hooks and keys
    format.ts              INR formatting from integer paise (en-IN grouping, lakh/crore), bps, time in IST
    status.ts              labels, tones and icons for every status, source and outcome
    webauthn.ts            passkey helpers with human-readable errors
    mock/                  fixtures, simulation engine, simulated chat, mock API
```

## Design notes

- Dark-first trading terminal with a light theme. OKLCH tokens in `src/app/globals.css`: profit, loss, warning, danger, info, plus two authority accents: violet for what the AI may do and emerald for what only you may do.
- Hairline borders, 8 and 12 px radii, a 4 px grid, Geist Sans and Geist Mono with tabular numbers.
- Prices flash for 600 ms on each tick by swapping between two identical keyframes (no remount). Motion is short and purposeful and respects `prefers-reduced-motion`.
- Desktop: three panes (rail, conversation, right pane). Mobile: bottom tab bar, side panels become a bottom sheet, sticky composer, `dvh` units, safe-area insets and 44 px touch targets.
- Lightweight Charts keeps its attribution logo enabled, as its Apache-2.0 NOTICE requires. Chart labels are rendered in IST.

## Contract notes and assumptions

These follow `docs/API.md`; where the contract is silent the UI assumes:

- `GET /api/events` sends named events (`event: tick` and so on). Unnamed frames shaped `{ "type": ..., "data": ... }` are also accepted.
- `GET /api/demo/state` failing (any error) means demo mode is off, and the Presenter button is hidden.
- `GET /api/me` returning 401 or 403 means "not signed in" and shows the first-run screen; any other failure shows a "Cannot reach the Countersign API" screen.
- Approval start errors for an unset PIN or TOTP may use codes outside the documented list (the mock uses `PIN_NOT_SET` and `TOTP_NOT_SET`); the UI shows the backend message for any code.
- FastAPI-style error bodies (`{ "detail": ... }`) are also understood, in addition to `{ code, message, detail }`.
- The "Executor" health dot warns when its heartbeat is older than 20 s.
- The "Alert me if X falls more than 5%" prompt is drafted by the backend; the mock models it as an LTP threshold 5% below the price at arming.
