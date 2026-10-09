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
    "What is my average purchase price for Reliance?",
    "Show NIFTY options near the money",
    "Buy 10 RELIANCE at 2800",
    "Alert me if TATAMOTORS falls more than 5%",
]


def handle(d: ChatDeps) -> str:
    parsed = parse(d.user_text)
    k = parsed.kind
    if k == "query":
        return _query(d, parsed.query, parsed.args)
    if k == "clarify":
        T.ask_clarification(d, parsed.question or "Could you clarify?", parsed.options)
        return "I need one detail before I can prepare anything."
    acct = T._account(d)
    with session_scope() as s:
        if k in ("order", "cancel", "modify"):
            out = proposals.create_order(s, parsed.order, user_text=d.user_text, account=acct, user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin, facts=parsed.facts)
        elif k == "plan":
            out = proposals.create_plan(s, parsed.plan, user_text=d.user_text, account=acct, user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin)
        elif k == "mandate":
            out = proposals.create_mandate(s, parsed.mandate, user_text=d.user_text, account=acct, user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin)
        else:
            d.emit({"kind": "notice", "tone": "info", "title": "Try one of these", "message": " | ".join(SUGGESTIONS)})
            return "I can answer account and market questions, and prepare orders, plans and standing instructions for you to countersign."
        res = T._record(d, out)
    if res.get("status") == "NEEDS_CLARIFICATION":
        return "I need one detail before I can prepare this."
    if res["status"] == "BLOCKED":
        return f"I prepared the canonical {('plan' if k == 'plan' else 'instruction' if k == 'mandate' else 'order')}, but the verifier blocked it. The card shows exactly why; nothing can be approved until it is fixed."
    if k == "mandate":
        return "Here is the exact standing instruction. It fires at most once, only within the scope you sign. Countersign it with your passkey to arm it."
    if k == "plan":
        return "Here is the complete plan. One countersignature binds every step; if a step fails, later steps stop and no compensating trades are made."
    return "Here is the exact order the executor would send. Review every field and its source, then countersign it with your passkey. The approval expires in 60 seconds."


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
        r = T.get_holdings(d, losers_below_pct=th)
        if not r["count"]:
            return f"None of your holdings are down more than {th:g}% from your average price."
        names = ", ".join(f"{h['symbol']} ({h['pnl_pct']:+.2f}%)" for h in r["holdings"])
        return f"{r['count']} holding(s) are down more than {th:g}%: {names}."
    if q == "avg_price":
        syms = [s for s in args.get("symbols", []) if s.lower() not in ("average", "purchase", "price", "buy", "what", "is", "for", "of", "cost", "avg", "me")]
        for sym in syms:
            r = T.get_holdings(d, symbol=_canon_symbol(sym))
            if r["count"]:
                h = r["holdings"][0]
                return f"Your average purchase price for {h['symbol']} is {h['avg_price']} on {h['qty']} shares (LTP {h['ltp']}, P&L {h['pnl']})."
        T.get_holdings(d)
        return "I could not match that to one of your holdings; here are all your holdings with average prices."
    if q == "holdings":
        r = T.get_holdings(d)
        return f"You hold {r['count']} stocks."
    if q == "positions":
        r = T.get_positions(d)
        return f"You have {len(r['positions'])} open position(s)."
    if q == "option_chain":
        r = T.get_option_chain(d, width=args.get("width", 6))
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
    from countersign.core import instruments
    from countersign.core.db import session_scope as ss

    with ss() as sess:
        r = instruments.resolve(sess, s)
        return r.instrument.symbol if r.instrument else s


__all__ = ["handle", "inr"]
