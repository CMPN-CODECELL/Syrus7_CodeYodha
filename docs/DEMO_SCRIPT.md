# Judge demo script (about 10 minutes)

Before judges arrive: `uv run --project backend python scripts/dev.py` with the 021 sandbox configured (see `SETUP.md`, section 3b), then open **http://localhost:3000** or the HTTPS tunnel address (a passkey belongs to the host it was registered on). Part 1 signs up a new visitor, who gets a paper account. For the 021 parts, sign in with the team's demo login, whose email is listed in `CS_021_OWNER_EMAILS` (its credentials are kept outside the repository); it lands on the real 021 sandbox account, and the account menu (Broker connection) switches between it and its paper account. Prices on the 021 account are live (RELIANCE trades near Rs 1,200). On Saturday NSE is closed and the sandbox may not move or fill: run the standing-instruction trigger on a paper account, where the presenter feed scenarios drive prices.

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
| 11 | *Which positions are down more than 5%?* | Holdings and positions cards, measured from your average price ("today" measures from yesterday's close) |
| 12 | *What did I pay on average for INFY?* | Holding card with average price, quantity and P&L |
| 13 | *Show NIFTY options near the money for this expiry* | Option chain, ATM highlighted, lot size 65, Tuesday expiry; on 021 the prices are 021's live prices (a strike without a price yet shows none, never a simulated one) |
| 14 | *Buy 10 Reliance at market* | (If a hosted model asks which product, answer it or switch the engine to **Deterministic**.) The card shows the worst-case protected price (the market order is sent to 021 as a limit at that signed price). The approval card is built from the canonical order, not AI text. Point at provenance chips (your instruction / instrument metadata / live feed / risk engine), worst-case price, margin, charges, resulting position, band meter, order id = hash |
| 15 | Countersign with the passkey | Windows Hello prompt. "The OS verifies me; the passkey signs the hash of this exact order, nonce and a 60-second expiry." |
| 16 | Live outcomes | Signature verified, single-use capability, executor re-verified, sent once, broker acknowledged, filled (on 021 often in several small trades: the average is computed from the trades). Then *Sell half my TCS* (quantity computed from what you can sell) and *Cancel my ... order* |
| 17 | Price drift | On the paper account: draft an order, open Countersign, then **Demo controls** (account menu or `Ctrl+.`), feed `spike` on RELIANCE: "Approval invalidated because the price moved outside the approved band". (On the 021 account a scenario-driven price blocks the order instead: simulated prices never reach 021.) |
| 18 | Network drops mid-order | On the 021 account (gateway started with `CS_021_TEST_FAULTS=true`): Demo controls, **Connection fail**, countersign a small order: "Order not placed... Nothing was sent twice; countersign again to resend" and no new order in 021's order book. Then **Accept, drop ack**: 021 accepts, the answer is lost, outcome "Reconciled: not retried" and exactly one order |
| 19 | Standing instruction | *If Reliance drops below 2800, buy 10* (the official example): RELIANCE is far below 2800, so the copilot states the live price and asks: buy now, or a trigger below the price. Then on the paper account *Buy 100 ABCCORP if the price falls below 500*, countersign: the Mandates page shows ACTIVE. *Tell me when any holding falls 3% in a day* arms an alert that never trades |
| 20 | Trigger it | Demo controls, feed `dip_to_trigger` on ABCCORP (505, 500, 499): exactly one trigger, FIRED, filled once |
| 21 | Multi-step plan | *Rebalance so no single stock is more than 20% of my portfolio* or *Exit all my losing intraday positions*: one card, the arithmetic per step, one signature (use the PIN tab here to show the fallback) |
| 22 | Partial fill or rejection | On the paper account: Demo controls, broker fault `partial_fill` (or `reject`), approve a plan: the step shows partially filled with the remainder cancelled, or rejected with its reason (a step refused when sent skips the later ones), "No compensating trades" |

## Part 3: proof (about 2 minutes)
| # | Show | Say / point at |
|---|---|---|
| 23 | Attack Lab, **Run all attacks** | 9 scenarios stream their transcripts. Highlight **B (bribed model)** and **A (injection)** |
| 24 | Scoreboard | **Unapproved executions = 0** next to the naive copilot's unapproved orders (5 per full run) |
| 25 | Security Center | Your account panel, trust boundary (API holds write key: NO), invariant monitor all green (scenario I flagged as detected and contained), kill switch (engage, then release with your PIN). Security events show your own account by default; **With drills** adds the Attack Lab's blocked drills. Adding a passkey here first asks you to confirm with a method you already have: a password or a session alone can never add a way to countersign. |
| 26 | Audit, **Verify audit chain** | "Chain intact: N events verified from genesis" |
| 27 | Isolation (optional) | Sign out, sign up a second account: its own fresh portfolio, none of the first account's drafts or orders. Sign back in with the passkey: no email typed. |
| 28 | (Optional) Tests | `uv run pytest -q` (all pass); `python scripts/e2e_browser.py` (all checks pass); TLC: no error for Countersign, counterexample for naive retry |
| 29 | Close | "The AI can propose. The system can verify. The human must countersign. Only then can the executor act." |

To restart cleanly between judges: **Demo controls, Reset demo** resets only your own paper account and data (feed calm, faults cleared, sandboxes refilled). The 021 account cannot be reset from Countersign; cancel anything you placed there. If the LLM is slow or offline, switch the composer engine to **deterministic**: every flow above still works, and safety is identical.
