// Simulated POST /api/chat stream. A small deterministic parser stands in for the model:
// read-only questions return cards; trade instructions become proposals in the mock engine.
// The chat never "places" anything; it can only create a proposal that needs a countersignature.

import type { Card, ChatEngine, ChatEvent, ChatHistoryItem, Proposal } from "../types";
import { engine, rs, sleep } from "./engine";
import {
  EXPIRIES,
  INSTRUMENTS,
  buildHolding,
  buildOptionChain,
  buildPosition,
  buildStep,
  finalizeProposal,
  makeTick,
  mandateView,
  newProposalId,
  OPTION_POSITION,
} from "./fixtures";

const ALIASES: [RegExp, string][] = [
  [/\b(reliance|ril)\b/i, "RELIANCE"],
  [/\b(infy|infosys)\b/i, "INFY"],
  [/\btcs\b/i, "TCS"],
  [/\b(hdfcbank|hdfc bank|hdfc)\b/i, "HDFCBANK"],
  [/\b(tatamotors|tata motors|tatamotor)\b/i, "TATAMOTORS"],
  [/\b(sbin|sbi|state bank)\b/i, "SBIN"],
  [/\bitc\b/i, "ITC"],
  [/\bnifty\b/i, "NIFTY"],
];

function findSymbol(text: string): string | null {
  for (const [re, sym] of ALIASES) if (re.test(text)) return sym;
  return null;
}

function lev(a: string, b: string): number {
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return dp[a.length][b.length];
}

function lookalike(text: string): { input: string; symbol: string } | null {
  const tokens = text.match(/[A-Za-z0-9]{3,}/g) ?? [];
  for (const t of tokens) {
    if (/^(buy|sell|at|and|if|when|price|falls|below|above|alert|market)$/i.test(t)) continue;
    for (const sym of Object.keys(INSTRUMENTS)) {
      const d = lev(t.toUpperCase(), sym);
      if (d === 1 && t.toUpperCase() !== sym) return { input: t, symbol: sym };
      // case-confusable: lowercase l inside an otherwise uppercase symbol
      if (t !== sym && t.toUpperCase().replace(/L/g, "I") === sym.replace(/L/g, "I") && /[a-z]/.test(t) && /[A-Z]{3}/.test(t)) {
        return { input: t, symbol: sym };
      }
    }
  }
  return null;
}

function engineLabel(e: ChatEngine | undefined): string {
  switch (e) {
    case "ollama":
    case "auto":
    case undefined:
      return "ollama:qwen3:4b-instruct-2507";
    case "gemini":
      return "gemini:gemini-2.5-flash";
    case "groq":
      return "groq:llama-3.3-70b-versatile";
    default:
      return "deterministic";
  }
}

const parseNum = (s: string) => Number(s.replace(/,/g, ""));

interface Leg {
  side: "BUY" | "SELL";
  qty: number | null;
  symbol: string | null;
  price: number | null;
  raw: string;
}

function parseLegs(text: string): Leg[] {
  const parts = text.split(/\band\b|,|;|\bthen\b/i).map((p) => p.trim()).filter(Boolean);
  const legs: Leg[] = [];
  for (const part of parts) {
    const side = /\bsell\b/i.test(part) ? "SELL" : /\bbuy\b/i.test(part) ? "BUY" : null;
    if (!side) continue;
    const q = part.match(/\b(?:buy|sell)\s+(\d[\d,]*)/i);
    const priceM = part.match(/(?:\bat\b|@)\s*(?:rs\.?|₹|inr)?\s*(\d[\d,]*(?:\.\d+)?)/i);
    legs.push({ side, qty: q ? parseNum(q[1]) : null, symbol: findSymbol(part), price: priceM ? parseNum(priceM[1]) : null, raw: part });
  }
  return legs;
}

export interface MockChatStore {
  history: Map<string, ChatHistoryItem[]>;
}
const store: MockChatStore = { history: new Map() };

export function chatHistory(conversationId: string): ChatHistoryItem[] {
  return store.history.get(conversationId) ?? [];
}

export async function simulateChat(
  req: { conversation_id?: string; message: string; engine?: ChatEngine },
  emit: (e: ChatEvent) => void,
  signal?: AbortSignal,
) {
  const conversation_id = req.conversation_id ?? `conv_${Date.now().toString(36)}`;
  const message_id = `msg_${Date.now().toString(36)}`;
  const eng = engineLabel(req.engine);
  let text = "";
  const cards: Card[] = [];

  const out = (e: ChatEvent) => {
    if (signal?.aborted) return;
    if (e.type === "text-delta") text += e.delta;
    if (e.type === "card") cards.push(e.card);
    emit(e);
  };
  const say = async (s: string) => {
    const words = s.split(/(\s+)/);
    for (let i = 0; i < words.length; i += 3) {
      out({ type: "text-delta", delta: words.slice(i, i + 3).join("") });
      await sleep(28, signal);
    }
  };
  let toolN = 0;
  const tool = async (name: string, label: string, ms = 420, detail?: string) => {
    toolN += 1;
    const id = `tool_${toolN}`;
    out({ type: "tool", id, name, label, status: "running" });
    await sleep(ms, signal);
    out({ type: "tool", id, name, label, status: "done", detail });
  };

  await sleep(220, signal);
  if (req.engine === "groq") {
    out({ type: "meta", conversation_id, engine: eng, message_id });
    out({
      type: "error",
      code: "ENGINE_UNAVAILABLE",
      message: "Groq is not configured on this server (no API key set). Choose Auto, Ollama, Gemini or Deterministic.",
    });
    return;
  }
  out({ type: "meta", conversation_id, engine: eng, message_id });

  const msg = req.message.trim();
  const lower = msg.toLowerCase();
  const s = engine.s;

  const holdings = () => s.holdings.map((h) => buildHolding(h, s.prices[h.symbol].ltp, s.prices[h.symbol].prev));

  // Read-only intents ---------------------------------------------------------
  if (/\b(p&l|pnl|p and l|profit|loss today|how am i doing)\b/i.test(lower) && !/\b(buy|sell)\b/i.test(lower)) {
    await tool("get_account_summary", "Reading account summary...", 380);
    await tool("get_positions", "Reading positions...", 300);
    const sum = engine.summary();
    await say(
      `Your day P&L is **${sign(sum.day_pnl_paise)}** and total P&L is **${sign(sum.total_pnl_paise)}** across ${sum.holdings_count} holdings and ${sum.positions_count} open positions. Available margin is ${rs(sum.available_margin_paise)}.`,
    );
    out({ type: "card", card: { kind: "account_summary", data: sum } });
  } else if (/\b(down|fallen|losing|losers?)\b.*\d+\s*%|\bdown more than\b/i.test(lower)) {
    const pctM = lower.match(/(\d+(?:\.\d+)?)\s*%/);
    const pct = pctM ? Number(pctM[1]) : 5;
    await tool("get_holdings", "Reading holdings...", 420);
    await tool("get_positions", "Reading positions...", 260);
    const losers = holdings().filter((h) => h.pnl_bps <= -pct * 100);
    if (losers.length) {
      await say(
        `${losers.length === 1 ? "One holding is" : `${losers.length} holdings are`} down more than ${pct}% from your average price: ${losers
          .map((h) => `**${h.instrument.symbol}** (${(h.pnl_bps / 100).toFixed(2)}%)`)
          .join(", ")}. No open position is down more than ${pct}%.`,
      );
      out({ type: "card", card: { kind: "holdings", title: `Down more than ${pct}% from average`, data: losers } });
    } else {
      await say(`Nothing in your holdings or positions is down more than ${pct}% from its average price.`);
    }
  } else if (/\baverage\b.*\bprice\b|\bavg\b|\bbought at\b/i.test(lower)) {
    const sym = findSymbol(msg) ?? "RELIANCE";
    await tool("get_holdings", "Reading holdings...", 400);
    const h = holdings().find((x) => x.instrument.symbol === sym);
    if (h) {
      await say(
        `You hold **${h.quantity} ${sym}** at an average purchase price of **${rs(h.avg_price_paise)}**. LTP is ${rs(h.ltp_paise)}, so you are ${h.pnl_paise >= 0 ? "up" : "down"} ${Math.abs(h.pnl_bps / 100).toFixed(2)}% (${sign(h.pnl_paise)}).`,
      );
      out({ type: "card", card: { kind: "holdings", title: `${sym} holding`, data: [h] } });
    } else {
      await say(`You do not hold any ${sym} right now, so there is no average purchase price.`);
    }
  } else if (/\boption|chain|near the money|atm\b/i.test(lower)) {
    await tool("get_quote", "Reading NIFTY spot...", 300);
    await tool("get_option_chain", "Reading NIFTY option chain...", 520);
    const chain = buildOptionChain(s.prices.NIFTY.ltp, EXPIRIES[0], 6);
    await say(
      `NIFTY spot is ${rs(chain.spot_paise)}, so the at-the-money strike is **${(chain.atm_strike_paise / 100).toLocaleString("en-IN")}** for the ${chain.expiry} expiry. Lot size is ${chain.lot_size}. Here are the strikes near the money:`,
    );
    out({ type: "card", card: { kind: "option_chain", data: chain } });
  } else if (/\b(holdings|portfolio)\b/i.test(lower) && !/\b(buy|sell)\b/i.test(lower)) {
    await tool("get_holdings", "Reading holdings...", 420);
    await say(`You have ${s.holdings.length} holdings. Live prices update in the table below.`);
    out({ type: "card", card: { kind: "holdings", title: "Holdings", data: holdings() } });
  } else if (/\bpositions?\b/i.test(lower) && !/\b(buy|sell)\b/i.test(lower)) {
    await tool("get_positions", "Reading positions...", 380);
    const positions = s.positions.map((p) => buildPosition(p, p.inst.instrument_id === OPTION_POSITION.instrument_id ? s.optionLtp : s.prices[p.inst.symbol].ltp));
    await say(`You have ${positions.length} open positions.`);
    out({ type: "card", card: { kind: "positions", title: "Open positions", data: positions } });
  } else if (/\borders?\b/i.test(lower) && !/\b(buy|sell)\b/i.test(lower)) {
    await tool("get_orders", "Reading order book...", 380);
    await say(`Here is today's order book. Open orders are listed first.`);
    out({ type: "card", card: { kind: "orders", title: "Today's orders", data: [...s.orders].sort((a) => (a.status === "OPEN" ? -1 : 1)) } });
  } else if (/\b(quote|price of|ltp|how is|trading at)\b/i.test(lower) && !/\b(buy|sell|alert)\b/i.test(lower)) {
    const sym = findSymbol(msg) ?? "RELIANCE";
    await tool("get_quote", `Reading ${sym} quote...`, 380);
    const p = s.prices[sym];
    const tick = makeTick(INSTRUMENTS[sym], p.ltp, p.prev, p.vol, s.seq);
    await say(`${sym} is at ${rs(p.ltp)}, ${p.ltp >= p.prev ? "up" : "down"} ${Math.abs(((p.ltp - p.prev) / p.prev) * 100).toFixed(2)}% on the day.`);
    out({ type: "card", card: { kind: "quote", data: { instrument: INSTRUMENTS[sym], tick } } });
  }
  // Alerts ---------------------------------------------------------------------
  else if (/\b(alert|notify|tell me|warn me)\b/i.test(lower)) {
    const sym = findSymbol(msg);
    if (!sym) {
      out({ type: "card", card: { kind: "clarify", question: "Which instrument should the alert watch?", options: ["Alert me if TATAMOTORS falls more than 5%", "Alert me if RELIANCE falls more than 3%"] } });
      return finish();
    }
    await tool("resolve_instrument", "Resolving instrument...", 300, `${sym} on NSE`);
    await tool("get_quote", "Reading live quote...", 300);
    const ltp = s.prices[sym].ltp;
    const pctM = lower.match(/(\d+(?:\.\d+)?)\s*%/);
    const priceM = lower.match(/(?:below|under|above|over)\s*(?:rs\.?|₹)?\s*(\d[\d,]*(?:\.\d+)?)/);
    const up = /\b(rises?|above|over|up)\b/i.test(lower);
    const threshold = priceM ? Math.round(parseNum(priceM[1]) * 100) : Math.round((ltp * (1 - (up ? -1 : 1) * (pctM ? Number(pctM[1]) : 5) / 100)) / 5) * 5;
    await tool("draft_mandate", "Drafting standing alert for verification...", 480);
    const id = newProposalId();
    const condition = `LTP ${up ? ">=" : "<="} ${rs(threshold)}${pctM ? ` (${pctM[1]}% ${up ? "above" : "below"} ${rs(ltp)} at arming)` : ""}`;
    const p = finalizeProposal({
      id,
      kind: "MANDATE",
      status: "AWAITING_APPROVAL",
      summary: `ALERT WHEN ${condition} on ${sym}`,
      steps: [],
      mandate: null,
      failure_policy: null,
      issues: [{ code: "ALERT_ONLY", severity: "INFO", message: "Alert only: it notifies you and never places an order." }],
      can_approve: true,
      interpretation: { user_text: msg, engine: eng, disagreements: [] },
      approval: null,
      execution: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    p.mandate = mandateView(`mnd_${id.slice(4)}`, INSTRUMENTS[sym], {
      kind: "ALERT",
      op: up ? "GTE" : "LTE",
      threshold,
      template: null,
      state: "PENDING_APPROVAL",
      createdAgo: 0,
      expiresIn: 5 * 86_400_000,
      conditionText: condition,
      proposal_id: id,
    });
    engine.addProposal(p);
    await say(`I drafted a standing alert on **${sym}**. It only notifies you and cannot trade. Countersign it to arm it.`);
    out({ type: "card", card: { kind: "proposal", proposal_id: p.id, status: p.status } });
  }
  // Trades ---------------------------------------------------------------------
  else if (/\b(buy|sell)\b/i.test(lower)) {
    const look = lookalike(msg);
    if (look && !findSymbol(msg)) {
      await tool("resolve_instrument", "Resolving instrument...", 380, "No exact match");
      out({
        type: "card",
        card: {
          kind: "notice",
          tone: "danger",
          title: "Look-alike symbol blocked",
          message: `"${look.input}" is not a listed symbol but looks like ${look.symbol}. Nothing was drafted. If you meant ${look.symbol}, say so explicitly.`,
        },
      });
      out({ type: "card", card: { kind: "clarify", question: `Did you mean ${look.symbol}?`, options: [msg.replace(look.input, look.symbol), "No, cancel"] } });
      return finish();
    }
    const cond = lower.match(/\b(?:if|when)\b[^0-9]*?\b(below|under|above|over)\s*(?:rs\.?|₹)?\s*(\d[\d,]*(?:\.\d+)?)/);
    const legs = parseLegs(cond ? msg.split(/\b(?:if|when)\b/i)[0] : msg);
    const missingSym = legs.find((l) => !l.symbol);
    const missingQty = legs.find((l) => l.symbol && !l.qty);
    if (!legs.length || missingSym) {
      out({
        type: "card",
        card: {
          kind: "clarify",
          question: "Which instrument do you want to trade?",
          options: ["Buy 10 RELIANCE at 2800", "Buy 5 TCS at 3400", "Sell 20 INFY at 1530"],
        },
      });
      await say("I could not tell which instrument you meant, so nothing was drafted.");
      return finish();
    }
    if (missingQty) {
      const sym = missingQty.symbol as string;
      const verb = missingQty.side === "BUY" ? "Buy" : "Sell";
      out({
        type: "card",
        card: { kind: "clarify", question: `How many ${sym} shares should I ${verb.toLowerCase()}?`, options: [1, 5, 10].map((n) => `${verb} ${n} ${sym}${missingQty.price ? ` at ${missingQty.price}` : ""}`) },
      });
      await say("Quantity was missing. I never guess quantities, so please pick one.");
      return finish();
    }
    for (const l of legs) await tool("resolve_instrument", `Resolving ${l.symbol}...`, 260, `${INSTRUMENTS[l.symbol as string].display_name}`);
    await tool("get_quote", "Reading live quotes...", 320);
    await tool("draft_proposal", "Drafting order for verification...", 520);
    await tool("verify", "Verifier checking margin, price band and provenance...", 560);

    const specs = legs.map((l) => {
      const sym = l.symbol as string;
      const limit = cond ? Math.round(parseNum(cond[2]) * 100) : l.price ? Math.round(l.price * 100) : null;
      return engine.specFor({ side: l.side, symbol: sym, qty: l.qty as number, orderType: limit ? "LIMIT" : "MARKET", limit, userText: msg });
    });
    const id = newProposalId();
    let p: Proposal;
    if (cond) {
      const step = buildStep(specs[0], 1, null);
      const up = /above|over/.test(cond[1]);
      const threshold = Math.round(parseNum(cond[2]) * 100);
      p = finalizeProposal({
        id,
        kind: "MANDATE",
        status: "AWAITING_APPROVAL",
        summary: `WHEN LTP ${up ? ">=" : "<="} ${rs(threshold)}: ${step.summary}`,
        steps: [step],
        mandate: null,
        failure_policy: null,
        issues: [
          ...step.issues,
          { code: "STANDING_INSTRUCTION", severity: "INFO", message: "One-shot standing instruction. If the price crosses while the system is offline it moves to review instead of firing stale." },
        ],
        can_approve: true,
        interpretation: { user_text: msg, engine: eng, disagreements: [] },
        approval: null,
        execution: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
      p.mandate = mandateView(`mnd_${id.slice(4)}`, INSTRUMENTS[specs[0].symbol], {
        kind: "ORDER",
        op: up ? "GTE" : "LTE",
        threshold,
        template: step.canonical,
        state: "PENDING_APPROVAL",
        createdAgo: 0,
        expiresIn: 5 * 86_400_000,
        proposal_id: id,
      });
    } else if (specs.length > 1) {
      const steps = specs.map((sp, i) => buildStep(sp, i + 1, i === 0 ? null : i));
      p = finalizeProposal({
        id,
        kind: "PLAN",
        status: "AWAITING_APPROVAL",
        summary: `PLAN (${steps.length} steps): ${steps.map((st) => st.summary).join(", then ")}`,
        steps,
        mandate: null,
        failure_policy: "STOP_ON_FAILURE",
        issues: steps.flatMap((st) => st.issues),
        can_approve: true,
        interpretation: { user_text: msg, engine: eng, disagreements: [] },
        approval: null,
        execution: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
    } else {
      const step = buildStep(specs[0], 1, null);
      p = finalizeProposal({
        id,
        kind: "ORDER",
        status: "AWAITING_APPROVAL",
        summary: step.summary,
        steps: [step],
        mandate: null,
        failure_policy: null,
        issues: step.issues,
        can_approve: !step.issues.some((i) => i.severity === "BLOCK"),
        interpretation: { user_text: msg, engine: eng, disagreements: [] },
        approval: null,
        execution: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
    }
    engine.addProposal(p);
    const kindText = p.kind === "PLAN" ? `a ${p.steps.length}-step plan` : p.kind === "MANDATE" ? "a standing instruction" : "an order";
    await say(
      `I drafted ${kindText} from your words and the verifier checked it. **It cannot execute until you countersign it.** Every value below shows where it came from.`,
    );
    out({ type: "card", card: { kind: "proposal", proposal_id: p.id, status: p.status } });
  } else {
    await say(
      "I can read your account and the market, and draft orders, plans or standing instructions for you to countersign. I cannot place orders myself. Try one of these:",
    );
    out({
      type: "card",
      card: {
        kind: "clarify",
        question: "What would you like to do?",
        options: ["What is my P&L today?", "Show NIFTY options near the money", "Buy 10 RELIANCE at 2800"],
      },
    });
  }
  return finish();

  function finish() {
    out({ type: "done" });
    const list = store.history.get(conversation_id) ?? [];
    list.push({ id: `${message_id}_u`, role: "user", text: msg, cards: [], created_at: new Date().toISOString() });
    list.push({ id: message_id, role: "assistant", text, cards, created_at: new Date().toISOString() });
    store.history.set(conversation_id, list);
  }
}

function sign(paise: number): string {
  return `${paise >= 0 ? "+" : "-"}${rs(Math.abs(paise))}`;
}
