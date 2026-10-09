# Problem statement requirement -> implementation -> demo evidence

Official statement: 021-01 "Copilot, Not Autopilot" (our PS-04).

| Requirement | Implementation | Demo evidence |
|---|---|---|
| L1 read-only queries: today's P&L, positions down > 5%, average purchase price, NIFTY options near the money | `llm/tools.py` read tools via `ReadGateway` (READ key only) on the caller's active account (the real 021 account through `gateway021`, or a paper account); `core/portfolio.py`; deterministic router fallback; on 021 the option chain uses live 021 prices only | Demo steps 10-13; `/api/account/*`, `/api/market/option-chain`; `test_l2_l4_queries.py` |
| L2 place, modify, cancel from plain English | Draft tools -> `core/proposals.py` -> canonical `CanonicalOrder` (PLACE/MODIFY/CANCEL); market orders with a signed protection price; "sell half" computed from sellable quantity; stop-loss moves as a full-state MODIFY | Steps 14-16; *Buy 10 Reliance at market*, *Sell half my TCS*, *Move my stop-loss on HDFC Bank up to 1640* (works at fixture prices in the test; at sandbox prices near Rs 700 the official 1640 is refused because a sell trigger must be below the last price, so demo it with about 690 and a resting sell stop-loss), *Cancel my ITC order*; `test_l2_l4_orders.py` |
| Convert to an exact order, show it, send only after approval of that exact order | Builder + verifier + provenance; approval card from canonical JSON; WebAuthn over `sha256(RFC 8785(payload))`; executor re-verifies hash and signature | Approval card, Attack Lab C and H |
| Never cause a trade the trader did not clearly approve | No execution tools in the AI; credential zones; single-use capability; executor checks; monitor | Attack Lab A-I, UNAPPROVED EXECUTIONS = 0, TLA+ |
| Misread requests | Parser facts + provenance + disagreement block; clarify instead of guess | Attack Lab C; *Buy some Reliance* asks a question |
| Network failures, and a sandbox that returns rate limits, random 500/503 and timeouts | Paper: outbox, unique tags, reconcile-before-retry, DBOS recovery. 021: write-ahead attempt record, sent once, reconciled against the order book by fingerprint and send time, never retried by the executor (`NOT_PLACED` / `NEEDS_REVIEW`), GETs retried with backoff | Attack Lab E; step 18 (gateway fault drills); `test_021_gateway.py`, `test_021_executor.py` |
| Changing prices | Signed price band, 60 s expiry, market protection | Attack Lab D, G; step 9 |
| Malicious instructions in stock names | Vendor-text quarantine, look-alike detection, provenance | Attack Lab A; `RΕLIANCE` look-alike refused |
| L3 standing instructions with the live feed | Mandate engine on the tick stream (live 021 ticks from `gateway021`); an already-true condition is never armed silently; all-holdings day-change alert | Steps 19-20; `test_l2_l4_mandates.py`, `test_mandate_already_true.py` |
| Survive restarts, trigger only once | Durable mandates in Postgres, CAS trigger, unique trigger_event_id, NEEDS_REVIEW after downtime | `test_mandates.py`; restart the API between arming and triggering |
| L4 multi-step plans approved as a whole | One signature over `CanonicalPlan`; per-step state; dependency waits; exit-losers and rebalance plans with the arithmetic on the card; no compensating trades | Steps 21-22; `test_l2_l4_plans.py` |
| Report all outcomes incl. partial fills and rejections | Tracker applies broker updates (on 021 from REST after each order event, a catch-up on reconnect and a 3 s poll); plan step states; outcome feed, including fills after a confirmed cancellation | Step 22; Outcomes page |
| Web chat interface | Next.js 16 app: public landing page, sign-up and sign-in (password or passkey), onboarding with the trader's own PIN, then streaming chat with rich cards and the approval flow | Whole demo; `scripts/e2e_browser.py` |
| (Product) Real accounts, isolated | One paper trading account per user, seeded at sign-up; every API read, chat tool, event and approval scoped to the caller; per-account broker key scopes; executor refuses approvals for accounts the approver does not own | Demo step 27; `test_isolation.py`, `test_broker_accounts.py` |
| (Proposal USP) Unforgeable countersignature | WebAuthn platform passkey; PIN/TOTP fallbacks with server attestation | Step 7; Attack Lab B forged signature |
| (Proposal USP) Bribed-model proof | Attack Lab with compromised FunctionModel + naive baseline | Step 15-16 |
| (Proposal USP) Model-agnostic safety | Pydantic AI FallbackModel Groq -> Gemini -> Ollama (order set by `CS_LLM_ORDER`), deterministic interpreter as last resort and as the fast path for read-only queries; safety independent of model | Engine selector in the composer |
| (Proposal USP) Wallet-style preview | Margin, charges, resulting position, worst-case price | Approval card |
| (Proposal USP) Field provenance | `FieldRow.source` on every field; look-alike flags | Approval card chips |
| (Proposal USP) Self-auditing core | Hash-chained audit, independent monitor, TLA+ | Security Center, step 18-19 |
