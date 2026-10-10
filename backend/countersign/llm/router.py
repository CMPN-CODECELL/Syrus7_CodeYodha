"""Deterministic engine: answers read-only queries and drafts proposals from the parser's intents, with no model.
Used when no LLM is available, when the trader selects it, or as a fallback if the model fails."""

from __future__ import annotations

from countersign.core import proposals
from countersign.core.db import session_scope
from countersign.core.money import inr
from countersign.core.parser import parse
from countersign.llm import tools as T
from countersign.llm.tools import ChatDeps

SUGGESTIONS = [
    "What is my P&L today?",
    "Which positions are down more than 5%?",
    "What did I pay on average for INFY?",
    "Show NIFTY options near the money",
    "Buy 10 RELIANCE at market",
    "Sell half my TCS at market",
    "Move my stop-loss on HDFC Bank up to 1640",
    "If Reliance drops below 1150, buy 10",
    "Tell me when any holding falls 3% in a day",
    "Exit all my losing intraday positions",
    "Cancel all my open orders",
    "Rebalance so no single stock is more than 20% of my portfolio",
]

# Words that can never be a symbol in a question about an average price.
_NOT_SYMBOLS = {"average", "purchase", "price", "buy", "what", "is", "for", "of", "cost", "avg", "me", "did", "i", "pay", "paid", "on", "my", "the", "much", "how"}


def handle(d: ChatDeps) -> str:
    parsed = parse(d.user_text)
    k = parsed.kind
    if k == "query":
        return _query(d, parsed.query, parsed.args)
    if k == "clarify":
        T.ask_clarification(d, parsed.question or "Could you clarify?", parsed.options)
        return "I need one detail before I can prepare anything."
    acct = T._account(d)
    common = dict(user_text=d.user_text, account=acct, user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin)
    with session_scope() as s:
        if k in ("order", "cancel", "modify"):
            out = proposals.create_order(s, parsed.order, facts=parsed.facts, **common)
        elif k == "plan":
            out = proposals.create_plan(s, parsed.plan, **common)
        elif k == "mandate":
            out = proposals.create_mandate(s, parsed.mandate, **common)
        elif k == "exit_losers":
            out = proposals.create_exit_plan(s, **common)
        elif k == "cancel_all":
            out = proposals.create_cancel_all_plan(s, **common)
        elif k == "rebalance":
            out = proposals.create_rebalance_plan(s, max_pct=parsed.args.get("max_pct"), **common)
        else:
            d.emit({"kind": "notice", "tone": "info", "title": "Try one of these", "message": " | ".join(SUGGESTIONS)})
            return "I can answer account and market questions, and prepare orders, plans and standing instructions for you to countersign."
        res = T._record(d, out)
    if res.get("status") == "NEEDS_CLARIFICATION":
        return "I need one detail before I can prepare this."
    if res.get("status") == "NOTHING_DRAFTED":
        return res["message"]
    is_plan = k in ("plan", "exit_losers", "rebalance", "cancel_all")
    if res["status"] == "BLOCKED":
        return f"I prepared the canonical {('plan' if is_plan else 'instruction' if k == 'mandate' else 'order')}, but the verifier blocked it. The card shows exactly why; nothing can be approved until it is fixed."
    if k == "mandate":
        if parsed.mandate is not None and parsed.mandate.scope == "ALL_HOLDINGS":
            return "Here is the exact alert. It watches every stock you hold, fires at most once per holding per trading day, survives restarts and never places an order. Countersign it to arm it."
        return "Here is the exact standing instruction. It fires at most once, only within the scope you sign. Countersign it with your passkey to arm it."
    if k == "cancel_all":
        return "Here is the plan the verifier computed: one CANCEL step per open order. One countersignature binds every step; a partly filled order keeps what already filled, and nothing new is placed."
    if k in ("exit_losers", "rebalance"):
        return "Here is the plan the verifier computed, with the arithmetic on each step. One countersignature binds every step; I will report exactly what fills and what is rejected, and no compensating trades are made."
    if k == "plan":
        return "Here is the complete plan. One countersignature binds every step; if a step fails, only the steps that depend on it stop, and no compensating trades are made."
    return "Here is the exact order the executor would send. Review every field and its source, then countersign it with your passkey. The approval expires in 60 seconds."


def _resolve_symbol(sym: str) -> str | None:
    from countersign.core import instruments

    with session_scope() as sess:
        r = instruments.resolve(sess, sym)
        return r.instrument.symbol if r.status == "RESOLVED" and r.instrument else None


def _query(d: ChatDeps, q: str, args: dict) -> str:
    if q == "pnl":
        r = T.get_account_summary(d)
        return f"Today's P&L is {r['day_pnl_paise']} and your total unrealised P&L is {r['total_pnl_paise']}."
    if q == "margin":
        r = T.get_account_summary(d)
        return f"Available margin is {r['available_margin_paise']} (used {r['used_margin_paise']}, cash {r['cash_paise']})."
    if q == "exposure":
        r = T.get_account_summary(d)
        return f"Your current exposure is {r['exposure_paise']} across {r['holdings_count']} holdings and {r['positions_count']} positions."
    if q == "losers":
        th = args.get("threshold_pct", 5.0)
        basis = args.get("basis", "avg")
        how = "today (from the previous close)" if basis == "day" else "from your average price"
        r = T.get_holdings(d, losers_below_pct=th, basis=basis)
        parts = []
        if r["count"]:
            key = "day_change_pct" if basis == "day" else "pnl_pct"
            parts.append(f"{r['count']} holding(s): " + ", ".join(f"{h['symbol']} ({h[key]:+.2f}%)" for h in r["holdings"]))
        if args.get("scope", "both") == "both":
            p = T.get_positions(d, losers_below_pct=th, basis=basis)
            if p["count"]:
                parts.append(f"{p['count']} position(s): " + ", ".join(f"{x['symbol']} {x['product']} ({x['pnl_pct']:+.2f}%)" for x in p["positions"]))
        if not parts:
            return f"Nothing you hold is down more than {th:g}% {how}."
        return f"Down more than {th:g}% {how}: " + "; ".join(parts) + "."
    if q == "avg_price":
        syms = [s for s in args.get("symbols", []) if s.lower() not in _NOT_SYMBOLS]
        sym = next((r for r in (_resolve_symbol(s) for s in syms) if r), None)
        if sym is None:
            T.get_holdings(d)
            return "I could not match that to one of your holdings; here are all your holdings with average prices."
        r = T.get_holdings(d, symbol=sym)
        if r["count"]:
            h = r["holdings"][0]
            return f"You paid {h['avg_price']} on average for {h['symbol']} ({h['qty']:,} shares in delivery). It trades at {h['ltp']} now, P&L {h['pnl']}."
        p = T.get_positions(d, symbol=sym)
        if p["count"]:
            x = p["positions"][0]
            return f"Your {x['symbol']} {x['product']} position of {x['qty']:,} has an average price of {x['avg']} (now {x['ltp']}, P&L {x['pnl']})."
        return f"You do not hold {sym}, in delivery or as an open position."
    if q == "holdings":
        r = T.get_holdings(d)
        return f"You hold {r['count']} stocks."
    if q == "positions":
        r = T.get_positions(d)
        return f"You have {len(r['positions'])} open position(s)."
    if q == "option_chain":
        r = T.get_option_chain(d, width=args.get("width", 6))
        if "error" in r:
            return r["error"].split(" Tell the trader")[0]
        return f"NIFTY spot is {r['spot']}; the at-the-money strike for the {r['expiry']} expiry is {r['atm_strike']} (lot size {r['lot_size']})."
    if q in ("open_orders", "rejected_orders"):
        r = T.get_orders(d, "rejected" if q == "rejected_orders" else "open")
        n = len(r["orders"])
        return f"You have {n} {'rejected' if q == 'rejected_orders' else 'open'} order(s)." if n else f"No {'rejected' if q == 'rejected_orders' else 'open'} orders."
    if q == "quote":
        for sym in args.get("symbols", []):
            r = T.get_quote(d, _canon_symbol(sym))
            if "error" not in r:
                return f"{r['symbol']} is trading at {r['ltp']} ({r['change_pct']:+.2f}% today)."
        return "Which symbol? For example 'price of INFY'."
    return "I could not understand that."


def _canon_symbol(s: str) -> str:
    return _resolve_symbol(s) or s


__all__ = ["handle", "inr"]
