# PS-04 requirement -> implementation -> demo evidence

| PS-04 requirement | Implementation | Demo evidence |
|---|---|---|
| Read-only queries: today's P&L, positions down > 5%, average purchase price, NIFTY options near the money | `llm/tools.py` read tools via `ReadGateway` (READ key only); `core/portfolio.py`; deterministic router fallback | Demo steps 2-5; `/api/account/*`, `/api/market/option-chain` |
| Place, modify, cancel from plain English | Draft tools -> `core/proposals.py` -> canonical `CanonicalOrder` (PLACE/MODIFY/CANCEL) | Steps 6-8; *modify my INFY order to 1850*, *cancel my INFY order* |
| Convert to an exact order, show it, send only after approval of that exact order | Builder + verifier + provenance; approval card from canonical JSON; WebAuthn over `sha256(RFC 8785(payload))`; executor re-verifies hash and signature | Approval card, Attack Lab C and H |
| Never cause a trade the trader did not clearly approve | No execution tools in the AI; credential zones; single-use capability; executor checks; monitor | Attack Lab A-I, UNAPPROVED EXECUTIONS = 0, TLA+ |
| Misread requests | Parser facts + provenance + disagreement block; clarify instead of guess | Attack Lab C; *Buy some Reliance* asks a question |
| Network failures | Outbox, unique tags, reconcile-before-retry, DBOS recovery | Attack Lab E; step 10; `test_timeout_after_accept...` |
| Changing prices | Signed price band, 60 s expiry, market protection | Attack Lab D, G; step 9 |
| Malicious instructions in stock names | Vendor-text quarantine, look-alike detection, provenance | Attack Lab A; `RΕLIANCE` look-alike refused |
| Standing instructions with live feed | Mandate engine on the tick stream | Steps 11-12 |
| Survive restarts, trigger only once | Durable mandates in Postgres, CAS trigger, unique trigger_event_id, NEEDS_REVIEW after downtime | `test_mandates.py`; restart the API between arming and triggering |
| Multi-step plans approved as a whole | One signature over `CanonicalPlan`; per-step state; dependency waits; stop on failure | Step 13 |
| Report all outcomes incl. partial fills and rejections | Tracker applies broker postbacks; plan step states; outcome feed | Step 14; Outcomes page |
| Web chat interface | Next.js 16 app: public landing page, sign-up and sign-in (password or passkey), onboarding with the trader's own PIN, then streaming chat with rich cards and the approval flow | Whole demo; `scripts/e2e_browser.py` |
| (Product) Real accounts, isolated | One paper trading account per user, seeded at sign-up; every API read, chat tool, event and approval scoped to the caller; per-account broker key scopes; executor refuses approvals for accounts the approver does not own | Demo step 27; `test_isolation.py`, `test_broker_accounts.py` |
| (Proposal USP) Unforgeable countersignature | WebAuthn platform passkey; PIN/TOTP fallbacks with server attestation | Step 7; Attack Lab B forged signature |
| (Proposal USP) Bribed-model proof | Attack Lab with compromised FunctionModel + naive baseline | Step 15-16 |
| (Proposal USP) Model-agnostic safety | Pydantic AI FallbackModel Groq -> Gemini -> Ollama (order set by `CS_LLM_ORDER`), deterministic interpreter as last resort and as the fast path for read-only queries; safety independent of model | Engine selector in the composer |
| (Proposal USP) Wallet-style preview | Margin, charges, resulting position, worst-case price | Approval card |
| (Proposal USP) Field provenance | `FieldRow.source` on every field; look-alike flags | Approval card chips |
| (Proposal USP) Self-auditing core | Hash-chained audit, independent monitor, TLA+ | Security Center, step 18-19 |
