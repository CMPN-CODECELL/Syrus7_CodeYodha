"""The AI's entire capability surface (tool allowlist). Read tools return structured data with sanitized names only;
draft tools create proposals that a human must countersign. There is no tool that places, modifies, cancels or
approves anything, and the API process holds no broker write credentials."""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from countersign.core import portfolio, proposals
from countersign.core.broker_client import ReadGateway, read_gateway
from countersign.core.db import session_scope
from countersign.core.money import inr
from countersign.core.parser import clauses, extract_facts, facts_for
from countersign.core.schemas import MandateIntent, OrderIntent, OrderType, PlanIntent, Product, Side
from countersign.core.verifier import AccountState

READ_TOOLS = [
    "get_account_summary",
    "get_holdings",
    "get_positions",
    "get_orders",
    "get_quote",
    "get_option_chain",
    "find_instrument",
]
DRAFT_TOOLS = ["propose_order", "propose_cancel", "propose_modify", "propose_plan", "propose_standing_instruction", "ask_clarification"]


@dataclass
class ChatDeps:
    user_id: str
    account_id: str
    conversation_id: str
    user_text: str
    engine: str
    emit: Callable[[dict], None]
    origin: str = "chat"
    gateway: ReadGateway | None = None  # defaults to a read-only client bound to this trader's own account
    tool_log: list[dict] = field(default_factory=list)
    proposal_ids: list[str] = field(default_factory=list)
    expose_raw_names: bool = False  # only the Attack Lab sets this, to prove even a poisoned model cannot trade
    draft_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.gateway is None:
            self.gateway = read_gateway(self.account_id)


def _account(d: ChatDeps) -> AccountState:
    return AccountState.load(d.gateway, d.account_id)


def get_account_summary(d: ChatDeps) -> dict:
    with session_scope() as s:
        data = portfolio.summary(s, d.gateway, d.account_id)
    d.emit({"kind": "account_summary", "data": data})
    return {k: (inr(v) if k.endswith("_paise") else v) for k, v in data.items()}


def get_holdings(d: ChatDeps, losers_below_pct: float | None = None, symbol: str | None = None) -> dict:
    with session_scope() as s:
        rows = portfolio.holdings(s, d.gateway)
    title = "Holdings"
    if losers_below_pct is not None:
        rows = [h for h in rows if h["pnl_bps"] <= -int(losers_below_pct * 100)]
        title = f"Holdings down more than {losers_below_pct:g}% from your average price"
    if symbol:
        su = symbol.upper()
        rows = [h for h in rows if h["instrument"]["symbol"] == su or su in h["instrument"]["display_name"].upper()]
        title = f"Holding: {symbol.upper()}"
    d.emit({"kind": "holdings", "title": title, "data": rows})
    return {
        "count": len(rows),
        "holdings": [
            {"symbol": h["instrument"]["symbol"], "qty": h["quantity"], "avg_price": inr(h["avg_price_paise"]), "ltp": inr(h["ltp_paise"]), "pnl": inr(h["pnl_paise"], True), "pnl_pct": round(h["pnl_bps"] / 100, 2)}
            for h in rows
        ],
    }


def get_positions(d: ChatDeps) -> dict:
    with session_scope() as s:
        rows = portfolio.positions(s, d.gateway)
    d.emit({"kind": "positions", "title": "Open positions", "data": rows})
    return {"positions": [{"symbol": p["instrument"]["symbol"], "product": p["product"], "qty": p["quantity"], "avg": inr(p["avg_price_paise"]), "ltp": inr(p["ltp_paise"]), "pnl": inr(p["pnl_paise"], True)} for p in rows]}


def get_orders(d: ChatDeps, status: str = "open") -> dict:
    status = status if status in ("open", "all", "rejected") else "open"
    with session_scope() as s:
        rows = portfolio.orders(s, d.gateway, status)
    d.emit({"kind": "orders", "title": {"open": "Open orders", "all": "All orders today", "rejected": "Rejected orders"}[status], "data": rows})
    return {"orders": [{"broker_order_id": o["broker_order_id"], "symbol": o["instrument"]["symbol"], "side": o["side"], "qty": o["quantity"], "filled": o["filled_quantity"], "status": o["status"], "reason": o.get("reject_reason")} for o in rows]}


def get_quote(d: ChatDeps, symbol: str) -> dict:
    with session_scope() as s:
        q = portfolio.quote(s, symbol)
        if q is None:
            return {"error": f"No unique instrument or live price for '{symbol}'. Ask the trader for the exact symbol."}
        if d.expose_raw_names:
            from countersign.core.models import Instrument

            q["instrument"]["vendor_description"] = s.get(Instrument, q["instrument"]["instrument_id"]).raw_name
    d.emit({"kind": "quote", "data": q})
    t = q["tick"]
    out = {"symbol": q["instrument"]["symbol"], "instrument_id": q["instrument"]["instrument_id"], "name": q["instrument"]["display_name"], "ltp": inr(t["ltp_paise"]), "change_pct": round(t["change_bps"] / 100, 2)}
    if "vendor_description" in q["instrument"]:
        out["vendor_description"] = q["instrument"]["vendor_description"]
    return out


def get_option_chain(d: ChatDeps, expiry: str | None = None, width: int = 6) -> dict:
    with session_scope() as s:
        oc = portfolio.option_chain(s, expiry, max(2, min(width, 12)))
    d.emit({"kind": "option_chain", "data": oc})
    atm = [r for r in oc["rows"] if abs(r["distance"]) <= 2]
    return {
        "expiry": oc["expiry"],
        "spot": inr(oc["spot_paise"]),
        "atm_strike": oc["atm_strike_paise"] // 100,
        "lot_size": oc["lot_size"],
        "near_the_money": [{"strike": r["strike_paise"] // 100, "CE": inr(r["ce"]["ltp_paise"]), "PE": inr(r["pe"]["ltp_paise"])} for r in atm],
    }


def find_instrument(d: ChatDeps, query: str) -> dict:
    from countersign.core import instruments as I

    with session_scope() as s:
        r = I.resolve(s, query)
        cands = [r.instrument] if r.instrument else r.candidates
        return {
            "status": r.status,
            "message": r.message,
            "matches": [{"instrument_id": c.instrument_id, "symbol": c.symbol, "name": c.display_name, "flags": list(c.flags or [])} for c in cands[:6]],
        }


def _already(d: ChatDeps) -> dict | None:
    if d.proposal_ids:
        return {"status": "ALREADY_DRAFTED", "proposal_id": d.proposal_ids[0], "note": "One draft per message. The first draft is already shown to the trader; do not draft again."}
    return None


def _record(d: ChatDeps, out: proposals.Outcome) -> dict:
    if out.clarify:
        q, opts = out.clarify
        d.emit({"kind": "clarify", "question": q, "options": opts})
        return {"status": "NEEDS_CLARIFICATION", "question": q}
    p = out.proposal
    d.proposal_ids.append(p.id)
    d.emit({"kind": "proposal", "proposal_id": p.id, "status": p.status})
    codes = [i["code"] for sv in p.steps_view for i in sv["issues"] if i["severity"] == "BLOCK"] + [i["code"] for i in p.issues if i["severity"] == "BLOCK"]
    return {
        "proposal_id": p.id,
        "status": p.status,
        "summary": p.summary,
        "blocking_issues": codes,
        "note": "This is a DRAFT, not an order. It is shown to the trader as a canonical card. Tell the trader it is ready for their countersignature (or why it is blocked). Never say it was approved, placed or executed.",
    }


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def propose_order(
    d: ChatDeps,
    side: str,
    symbol: str,
    quantity: int,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    trigger_price: float | None = None,
    product: str | None = None,
) -> dict:
    if (prev := _already(d)) is not None:
        return prev
    if len(clauses(d.user_text)) > 1:
        return {
            "error": "The trader asked for several actions in one message. Do not draft them separately: call propose_plan ONCE with every step so a single countersignature binds the whole plan.",
        }
    intent = OrderIntent(
        side=Side(side.upper()),
        instrument_ref=symbol,
        quantity=int(quantity),
        order_type=OrderType(order_type.upper()),
        limit_price=_dec(limit_price),
        trigger_price=_dec(trigger_price),
        product=Product(product.upper()) if product else None,
    )
    with session_scope() as s:
        out = proposals.create_order(s, intent, user_text=d.user_text, account=_account(d), user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin, facts=facts_for(d.user_text, symbol))
        return _record(d, out)


def propose_cancel(d: ChatDeps, order_ref: str) -> dict:
    if (prev := _already(d)) is not None:
        return prev
    with session_scope() as s:
        out = proposals.create_order(s, OrderIntent(action="CANCEL", target_order_ref=order_ref, instrument_ref=order_ref), user_text=d.user_text, account=_account(d), user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin)
        return _record(d, out)


def propose_modify(d: ChatDeps, order_ref: str, new_limit_price: float) -> dict:
    if (prev := _already(d)) is not None:
        return prev
    with session_scope() as s:
        out = proposals.create_order(s, OrderIntent(action="MODIFY", target_order_ref=order_ref, instrument_ref=order_ref, limit_price=_dec(new_limit_price)), user_text=d.user_text, account=_account(d), user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin)
        return _record(d, out)


def propose_plan(d: ChatDeps, steps: list[dict]) -> dict:
    if (prev := _already(d)) is not None:
        return prev
    intents = []
    for st in steps:
        action = (st.get("action") or "PLACE").upper()
        if action == "CANCEL":
            intents.append(OrderIntent(action="CANCEL", target_order_ref=st.get("order_ref") or st.get("symbol"), instrument_ref=st.get("symbol")))
            continue
        intents.append(
            OrderIntent(
                side=Side(str(st["side"]).upper()),
                instrument_ref=st["symbol"],
                quantity=int(st["quantity"]),
                order_type=OrderType(str(st.get("order_type", "LIMIT")).upper()),
                limit_price=_dec(st.get("limit_price")),
            )
        )
    with session_scope() as s:
        out = proposals.create_plan(s, PlanIntent(steps=intents), user_text=d.user_text, account=_account(d), user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin)
        return _record(d, out)


def propose_standing_instruction(
    d: ChatDeps,
    kind: str,
    symbol: str,
    condition: str = "LTP_BELOW",
    threshold_price: float | None = None,
    drop_pct: float | None = None,
    side: str | None = None,
    quantity: int | None = None,
    limit_price: float | None = None,
) -> dict:
    if (prev := _already(d)) is not None:
        return prev
    kind = kind.upper()
    order = None
    if kind == "ORDER":
        order = OrderIntent(side=Side(str(side).upper()), instrument_ref=symbol, quantity=int(quantity or 0), order_type=OrderType.LIMIT, limit_price=_dec(limit_price if limit_price is not None else threshold_price))
    mi = MandateIntent(
        kind="ORDER" if kind == "ORDER" else "ALERT",
        instrument_ref=symbol,
        field="DROP_FROM_AVG_BPS" if condition.upper() == "DROP_FROM_AVG" else "LTP",
        op="GTE" if condition.upper() in ("LTP_ABOVE", "DROP_FROM_AVG") else "LTE",
        threshold_price=_dec(threshold_price),
        drop_pct=_dec(drop_pct),
        order=order,
    )
    with session_scope() as s:
        out = proposals.create_mandate(s, mi, user_text=d.user_text, account=_account(d), user_id=d.user_id, engine=d.engine, conversation_id=d.conversation_id, origin=d.origin)
        return _record(d, out)


def ask_clarification(d: ChatDeps, question: str, options: list[str] | None = None) -> dict:
    d.emit({"kind": "clarify", "question": question, "options": options or []})
    return {"status": "ASKED"}


def _single_draft(fn):
    """Serialize draft tools per message: parallel tool calls from a model can never create two drafts."""

    @functools.wraps(fn)
    def wrapper(d: ChatDeps, *args, **kwargs):
        with d.draft_lock:
            return fn(d, *args, **kwargs)

    return wrapper


propose_order = _single_draft(propose_order)
propose_cancel = _single_draft(propose_cancel)
propose_modify = _single_draft(propose_modify)
propose_plan = _single_draft(propose_plan)
propose_standing_instruction = _single_draft(propose_standing_instruction)

TOOL_FUNCS = {f.__name__: f for f in (get_account_summary, get_holdings, get_positions, get_orders, get_quote, get_option_chain, find_instrument, propose_order, propose_cancel, propose_modify, propose_plan, propose_standing_instruction, ask_clarification)}
