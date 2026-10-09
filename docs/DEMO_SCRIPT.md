# Judge demo script (about 10 minutes)

Before judges arrive: `uv run --project backend python scripts/dev.py`, then open **http://localhost:3000** (use `localhost`: passkeys need it). Sign out of any earlier session so the demo starts on the landing page as a new visitor. Optionally create a spare account ahead of time in case the laptop's passkey prompt misbehaves. Nothing depends on live markets: prices come from the deterministic replay feed, and every account is a paper trading account on a simulated market.

## Part 1: first visit (about 3 minutes)
| # | Show | Say / point at |
|---|---|---|
| 1 | Landing page hero | "The AI proposes. You countersign." Point at the two chips: AI read and draft only, orders need your signature only. |
| 2 | Scroll: product cards | Hover the approval card fields (where each value came from), watch the price band void an approval when the price leaves it, open the signed payload and its hash. |
| 3 | How it works, Security, Attack Lab sections | The live panel reads `GET /api/public/status` from the running stack: unapproved executions 0, attacks blocked of attempted, audit events, systems up. No per-user data on a public page. |
| 4 | **Explore the Countersign Copilot** | Signed out, it asks you to sign in first. Click **Create an account**. |
| 5 | Sign up | Name, email, password. "Every new account gets its own paper trading account with a sample portfolio. No real money moves." |
| 6 | Onboarding: App PIN | Type `1234`: refused as too easy (the server refuses it too). Choose your own PIN. "Nobody presets it; a script never chose it." |
| 7 | Onboarding: Passkey | **Register passkey**, Windows Hello prompt. "Your fingerprint or face never leaves the device; Countersign stores only a public key." |
| 8 | Onboarding: Authenticator (optional) | Skip, or scan the QR with any TOTP app. The summary shows PIN, passkey and authenticator. "Signing in is not approval." **Enter the Copilot**. |

## Part 2: the copilot (about 5 minutes)
| # | Show | Say / point at |
|---|---|---|
| 9 | The header | One row: search, one status pill ("All systems normal", click it for broker, feed, executor, monitor, kill switch) and the account menu. The Copilot header carries the authority chips: "AI: propose only", "Execution: your countersignature", "Unapproved trades 0". |
| 10 | Chat: *What is my P&L today?* | Live account card; the AI used a read-only tool on **your** account |
| 11 | *Which positions are down more than 5%?* | TATAMOTORS card |
| 12 | *What is my average purchase price for Reliance?* | Holding card with average price |
| 13 | *Show NIFTY options near the money* | Option chain, ATM highlighted, lot size 65, Tuesday expiry |
| 14 | *Buy 10 RELIANCE at 2800 CNC* | (Stating the product keeps a hosted model from asking which one; the verifier would default to CNC anyway. If the AI still asks a question, answer it or switch the engine to **Deterministic**.) The approval card is built from the canonical order, not AI text. Point at provenance chips (your instruction / instrument metadata / live feed / risk engine), worst-case price, margin, charges, resulting position, band meter, order id = hash |
| 15 | Countersign with the passkey | Windows Hello prompt. "The OS verifies me; the passkey signs the hash of this exact order, nonce and a 60-second expiry." |
| 16 | Live outcomes | Signature verified, single-use capability, executor re-verified, sent with a unique tag, broker acknowledged, filled |
| 17 | Price drift | Draft another order, open Countersign, then **Demo controls** (account menu or `Ctrl+.`), feed `spike` on RELIANCE: "Approval invalidated because the price moved outside the approved band" |
| 18 | Timeout and reconciliation | Demo controls, broker fault `accept_then_drop`, approve an order: outcome "Reconciled: not retried" and exactly one order in Portfolio, Orders |
| 19 | Standing instruction | *Buy 100 ABCCORP if the price falls below 500*, countersign: the Mandates page shows ACTIVE |
| 20 | Trigger it | Demo controls, feed `dip_to_trigger` on ABCCORP (505, 500, 499): exactly one trigger, FIRED, filled once |
| 21 | Multi-step plan | *Sell 20 INFY at 1860 and buy 2 TCS at 4100*: one card, two steps, one signature (use the PIN tab here to show the fallback) |
| 22 | Partial fill or rejection | Demo controls, broker fault `partial_fill` (or `reject`), approve a plan: the step shows partially filled with the remainder cancelled, the next step SKIPPED, "No compensating trades" |

## Part 3: proof (about 2 minutes)
| # | Show | Say / point at |
|---|---|---|
| 23 | Attack Lab, **Run all attacks** | 9 scenarios stream their transcripts. Highlight **B (bribed model)** and **A (injection)** |
| 24 | Scoreboard | **Unapproved executions = 0** next to the naive copilot's unapproved orders (5 per full run) |
| 25 | Security Center | Your account panel, trust boundary (API holds write key: NO), invariant monitor all green (scenario I flagged as detected and contained), kill switch (engage, then release with your PIN). Security events show your own account by default; **With drills** adds the Attack Lab's blocked drills. Adding a passkey here first asks you to confirm with a method you already have: a password or a session alone can never add a way to countersign. |
| 26 | Audit, **Verify audit chain** | "Chain intact: N events verified from genesis" |
| 27 | Isolation (optional) | Sign out, sign up a second account: its own fresh portfolio, none of the first account's drafts or orders. Sign back in with the passkey: no email typed. |
| 28 | (Optional) Tests | `uv run pytest -q` 82 passed; `python scripts/e2e_browser.py` 27/27; TLC: no error for Countersign, counterexample for naive retry |
| 29 | Close | "The AI can propose. The system can verify. The human must countersign. Only then can the executor act." |

To restart cleanly between judges: **Demo controls, Reset demo** resets only your own paper account and data (feed calm, faults cleared, sandboxes refilled). If the LLM is slow or offline, switch the composer engine to **deterministic**: every flow above still works, and safety is identical.
