"""Deterministic interpreter for trader text.

Two jobs:
1. Misread guard: extract the facts the trader actually stated (side, quantity, price, symbols) so that any
   value proposed by the AI can be checked for provenance. A value the trader never stated is MODEL_INFERRED.
2. Offline engine: when no LLM is available (or it fails), route queries and build intents deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from countersign.core.schemas import MandateIntent, OrderIntent, OrderType, PlanIntent, Product, Side

NUM = r"(\d[\d,]*(?:\.\d+)?)"
CURRENCY = r"(?:₹|rs\.?|inr|rupees?)?\s*"
SYMBOL_STOP = {
    "BUY", "SELL", "SHARES", "SHARE", "OF", "AT", "THE", "A", "AN", "MY", "AND", "THEN", "IF", "WHEN", "PRICE", "FALLS",
    "BELOW", "ABOVE", "RISES", "GOES", "LIMIT", "MARKET", "ORDER", "ORDERS", "CANCEL", "MODIFY", "CHANGE", "TO", "FOR",
    "QTY", "QUANTITY", "LOT", "LOTS", "INTRADAY", "DELIVERY", "CNC", "MIS", "NRML", "ME", "ALERT", "NOTIFY", "IT",
    "MORE", "THAN", "BY", "PERCENT", "USE", "PROCEEDS", "PLACE", "NEW", "WITH", "SOME", "ALL", "MY", "OPEN", "PENDING",
    "PLEASE", "NOW", "ON", "RS", "INR", "AROUND", "ABOUT", "ROUGHLY", "APPROX", "UNDER", "OVER", "IOC", "DAY", "ONLY",
    "ONCE", "REACHES", "HITS", "DROPS", "HOLDING", "HOLDINGS", "POSITION", "S", "X", "SL", "STOP", "LOSS", "TRIGGER",
}
ROUGH_WORDS = re.compile(r"\b(around|about|roughly|approx(?:imately)?|near|some|few|couple|bunch|most|half)\b", re.I)


def _num(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


@dataclass
class Facts:
    """Everything the trader explicitly stated, used for provenance checks."""

    text: str
    sides: set[str] = field(default_factory=set)
    numbers: list[Decimal] = field(default_factory=list)
    quantity: int | None = None
    lots: int | None = None
    price: Decimal | None = None
    trigger: Decimal | None = None
    symbols: list[str] = field(default_factory=list)
    market: bool = False
    limit: bool = False
    product: Product | None = None
    rough: bool = False

    def mentions_number(self, value: Decimal | int | None) -> bool:
        if value is None:
            return False
        v = Decimal(str(value))
        return any(n == v for n in self.numbers)


@dataclass
class Parsed:
    kind: str  # query | order | modify | cancel | plan | mandate | clarify | unknown
    query: str | None = None
    args: dict = field(default_factory=dict)
    order: OrderIntent | None = None
    plan: PlanIntent | None = None
    mandate: MandateIntent | None = None
    question: str | None = None
    options: list[str] = field(default_factory=list)
    facts: Facts | None = None


def extract_facts(text: str) -> Facts:
    t = text.strip()
    low = t.lower()
    f = Facts(text=t)
    if re.search(r"\bbuy\b|\bpurchase\b|\bgo long\b|\badd\b", low):
        f.sides.add("BUY")
    if re.search(r"\bsell\b|\bexit\b|\bsquare off\b|\bdump\b", low):
        f.sides.add("SELL")
    f.numbers = [n for n in (_num(m) for m in re.findall(NUM, low)) if n is not None]
    m = re.search(r"\b(\d[\d,]*)\s*(lots?)\b", low)
    if m:
        f.lots = int(m.group(1).replace(",", ""))
    m = re.search(r"\b(?:buy|sell|purchase)\s+(\d[\d,]*)\b(?!\s*(?:%|percent))", low)
    if m and not f.lots:
        f.quantity = int(m.group(1).replace(",", ""))
    m = re.search(r"\b(\d[\d,]*)\s*(?:shares?|qty|quantity|units?)\b", low)
    if m and f.quantity is None and not f.lots:
        f.quantity = int(m.group(1).replace(",", ""))
    m = re.search(r"(?:\bat\b|@|\bprice\s+of\b|\blimit\b)\s*" + CURRENCY + NUM, low)
    if m:
        f.price = _num(m.group(1))
    m = re.search(r"\btrigger(?:\s+price)?\s*(?:at|of)?\s*" + CURRENCY + NUM, low)
    if m:
        f.trigger = _num(m.group(1))
    f.market = bool(re.search(r"\b(at\s+)?market(\s+price)?\b", low)) and "market data" not in low
    f.limit = bool(re.search(r"\blimit\b", low))
    if re.search(r"\b(intraday|mis)\b", low):
        f.product = Product.MIS
    elif re.search(r"\b(delivery|cnc|long term)\b", low):
        f.product = Product.CNC
    f.rough = bool(ROUGH_WORDS.search(low))
    f.symbols = _symbol_tokens(t)
    return f


def _symbol_tokens(text: str) -> list[str]:
    toks = re.findall(r"[^\s,;:!?()'\"]+", text)
    out = []
    for tok in toks:
        clean = tok.strip(".")
        if not clean or re.fullmatch(r"[\d,.%₹]+", clean):
            continue
        if clean.upper() in SYMBOL_STOP or len(clean) < 2:
            continue
        out.append(clean)
    return out


QUERY_RULES: list[tuple[str, re.Pattern]] = [
    ("rejected_orders", re.compile(r"\brejected\b.*\border|order.*\brejected\b", re.I)),
    ("open_orders", re.compile(r"\b(open|pending|outstanding)\s+orders?\b|\bmy orders\b|\border book\b", re.I)),
    ("losers", re.compile(r"\b(down|fallen|lost|losing|loss)\b.*\b(\d+(?:\.\d+)?)\s*(%|percent)|\blosers?\b|\bin the red\b", re.I)),
    ("avg_price", re.compile(r"\baverage\b.*\b(price|cost)\b|\bavg\b.*\bprice\b|\bbought\b.*\bat what\b", re.I)),
    ("option_chain", re.compile(r"\boption(s)?\b.*\b(chain|money|atm|nifty)|\bnifty\b.*\boptions?\b|\bnear the money\b|\batm\b", re.I)),
    ("margin", re.compile(r"\b(margin|funds|cash|buying power|available balance)\b", re.I)),
    ("exposure", re.compile(r"\bexposure\b", re.I)),
    ("pnl", re.compile(r"\bp\s*&\s*l\b|\bpnl\b|\bprofit\b|\bhow am i doing\b|\bmy (gains|returns)\b", re.I)),
    ("positions", re.compile(r"\bpositions?\b", re.I)),
    ("holdings", re.compile(r"\bholdings?\b|\bportfolio\b|\bwhat do i (own|hold)\b", re.I)),
    ("quote", re.compile(r"\b(price|quote|ltp|trading at|how is)\b", re.I)),
]

ACTION_RE = re.compile(r"\b(buy|sell|purchase|cancel|modify|change|alert|notify)\b", re.I)


def parse(text: str) -> Parsed:
    t = text.strip()
    low = t.lower()
    facts = extract_facts(t)

    if re.search(r"\b(get|buy|sell|want)\b.*\b(calls?|puts?|ce|pe)\b", low) and not re.search(r"\b\d{4,5}\s*(ce|pe|call|put)", low):
        return Parsed(
            kind="clarify",
            question="Which NIFTY option exactly? Tell me the strike, CE or PE, number of lots and limit price (for example 'buy 1 lot NIFTY 25400 CE at 120'). Ask 'show NIFTY options near the money' to see strikes.",
            options=["Show NIFTY options near the money"],
            facts=facts,
        )
    if re.search(r"\b(alert|notify|tell)\s+me\b", low) or re.search(r"\bif\b.*\b(falls?|drops?|rises?|goes|reaches|hits|crosses)\b", low):
        return _parse_mandate(t, facts)

    is_question = bool(re.match(r"^\s*(what|which|show|how|list|tell|give|display|any|do i|am i|where|get)\b", low)) or low.rstrip().endswith("?")
    if is_question and not re.search(r"\b(buy|sell|purchase)\s+\d", low):
        for name, rx in QUERY_RULES:
            if rx.search(t):
                args: dict = {}
                if name == "losers":
                    pct = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", low)
                    args["threshold_pct"] = float(pct.group(1)) if pct else 5.0
                if name in ("avg_price", "quote"):
                    args["symbols"] = facts.symbols
                if name == "option_chain":
                    args["width"] = 6
                return Parsed(kind="query", query=name, args=args, facts=facts)

    parts = re.split(r"\s*(?:,\s*)?\b(?:and then|then|and)\b\s*", t, flags=re.I)
    action_parts = [p for p in parts if ACTION_RE.search(p)]
    if len(action_parts) >= 2 and any(re.search(r"\b(buy|sell|cancel|place)\b", p, re.I) for p in action_parts):
        return _parse_plan(t, action_parts, facts)

    if re.search(r"\bcancel\b", low):
        return _parse_cancel(t, facts)
    if re.search(r"\b(modify|change|update|move)\b.*\border\b|\border\b.*\b(to|price)\b.*\d", low) and not re.search(r"\b(buy|sell)\b", low):
        return _parse_modify(t, facts)
    if re.search(r"\b(buy|sell|purchase)\b", low):
        return _parse_order(t, facts)

    for name, rx in QUERY_RULES:
        m = rx.search(t)
        if m:
            args: dict = {}
            if name == "losers":
                pct = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", low)
                args["threshold_pct"] = float(pct.group(1)) if pct else 5.0
            if name in ("avg_price", "quote"):
                args["symbols"] = facts.symbols
            if name == "option_chain":
                args["width"] = 6
            return Parsed(kind="query", query=name, args=args, facts=facts)
    return Parsed(kind="unknown", facts=facts)


def _order_from_text(t: str, facts: Facts) -> tuple[OrderIntent | None, str | None, list[str]]:
    low = t.lower()
    side = None
    if "BUY" in facts.sides and "SELL" not in facts.sides:
        side = Side.BUY
    elif "SELL" in facts.sides and "BUY" not in facts.sides:
        side = Side.SELL
    if side is None:
        return None, "Do you want to buy or sell?", ["Buy", "Sell"]
    sym = _pick_symbol(facts)
    if sym is None:
        return None, "Which instrument? Please give the exact trading symbol (for example RELIANCE).", []
    qty = facts.quantity
    if facts.lots:
        qty = None
    if facts.rough:
        return None, "Your request uses an approximate word (like 'some' or 'around'). Please state the exact quantity and price.", []
    if qty is None and not facts.lots:
        return None, f"How many shares of {sym.upper()} do you want to {side.value.lower()}? I will not guess a quantity.", []
    if facts.market:
        otype = OrderType.MARKET
        price = None
    elif facts.price is not None:
        otype = OrderType.LIMIT
        price = facts.price
    else:
        return (
            None,
            f"At what price? Give a limit price (for example 'at 2800') or say 'at market' for a price-protected market order.",
            ["At market (price-protected)"],
        )
    intent = OrderIntent(
        action="PLACE",
        side=side,
        instrument_ref=sym,
        quantity=qty,
        order_type=otype,
        limit_price=price,
        trigger_price=facts.trigger,
        product=facts.product,
    )
    if facts.lots:
        intent = intent.model_copy(update={"quantity": None})
        intent = intent.model_copy(update={"target_order_ref": f"LOTS:{facts.lots}"})
    return intent, None, []


def _pick_symbol(facts: Facts) -> str | None:
    opt = re.search(r"\bnifty\b\s*(\d{4,5})\s*(ce|pe|call|put)\b", facts.text, re.I)
    if opt:
        kind = "CE" if opt.group(2).lower() in ("ce", "call") else "PE"
        return f"NIFTYOPT:{opt.group(1)}:{kind}"
    cands = [s for s in facts.symbols if not re.fullmatch(r"\d+", s)]
    return cands[0] if cands else None


def _parse_order(t: str, facts: Facts) -> Parsed:
    intent, q, opts = _order_from_text(t, facts)
    if intent is None:
        return Parsed(kind="clarify", question=q, options=opts, facts=facts)
    return Parsed(kind="order", order=intent, facts=facts)


def _parse_cancel(t: str, facts: Facts) -> Parsed:
    m = re.search(r"\b(\d{6,})\b", t)
    ref = m.group(1) if m else _pick_symbol(facts)
    if not ref:
        return Parsed(kind="clarify", question="Which order should I cancel? Give the symbol or the broker order id.", facts=facts)
    return Parsed(kind="cancel", order=OrderIntent(action="CANCEL", target_order_ref=ref, instrument_ref=None if m else ref), facts=facts)


def _parse_modify(t: str, facts: Facts) -> Parsed:
    m = re.search(r"\b(\d{6,})\b", t)
    ref = m.group(1) if m else _pick_symbol(facts)
    price = None
    pm = re.search(r"\bto\s*" + CURRENCY + NUM, t.lower())
    if pm:
        price = _num(pm.group(1))
    if not ref or price is None:
        return Parsed(kind="clarify", question="Which order, and what new limit price? For example 'modify my INFY order to 1850'.", facts=facts)
    return Parsed(
        kind="modify",
        order=OrderIntent(action="MODIFY", target_order_ref=ref, instrument_ref=None if m else ref, limit_price=price),
        facts=facts,
    )


def _parse_plan(t: str, parts: list[str], facts: Facts) -> Parsed:
    steps: list[OrderIntent] = []
    last_cancelled_ref: str | None = None
    for p in parts:
        pf = extract_facts(p)
        pl = p.lower()
        if re.search(r"\bcancel\b", pl):
            c = _parse_cancel(p, pf)
            if c.kind != "cancel":
                return Parsed(kind="clarify", question=c.question, facts=facts)
            steps.append(c.order)
            last_cancelled_ref = c.order.target_order_ref
            continue
        if re.search(r"\bplace\b.*\border\b", pl) and last_cancelled_ref and pf.price is not None:
            steps.append(
                OrderIntent(action="PLACE", instrument_ref=None, limit_price=pf.price, order_type=OrderType.LIMIT, target_order_ref=f"REPLACE:{last_cancelled_ref}")
            )
            continue
        if re.search(r"\bproceeds\b", pl) and pf.quantity is None:
            sym = _pick_symbol(pf)
            return Parsed(
                kind="clarify",
                question=f"How many shares of {sym.upper() if sym else 'that stock'} should I buy with the proceeds? I will not compute or guess a quantity for you.",
                facts=facts,
            )
        intent, q, opts = _order_from_text(p, pf)
        if intent is None:
            return Parsed(kind="clarify", question=f"For the step '{p.strip()}': {q}", options=opts, facts=facts)
        steps.append(intent)
    return Parsed(kind="plan", plan=PlanIntent(steps=steps), facts=facts)


def _parse_mandate(t: str, facts: Facts) -> Parsed:
    low = t.lower()
    sym = None
    for s in facts.symbols:
        if s.lower() not in ("price", "holding", "holdings", "stock"):
            sym = s
            break
    pct = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", low)
    is_alert = bool(re.search(r"\b(alert|notify|tell)\s+me\b", low)) and not re.search(r"\b(buy|sell)\s+\d", low)
    if sym is None:
        return Parsed(
            kind="clarify",
            question="Which instrument should this standing instruction watch? Name the exact symbol (for example 'alert me if TATAMOTORS falls more than 5%').",
            facts=facts,
        )
    if is_alert:
        if pct:
            return Parsed(
                kind="mandate",
                mandate=MandateIntent(kind="ALERT", instrument_ref=sym, field="DROP_FROM_AVG_BPS", op="GTE", drop_pct=Decimal(pct.group(1))),
                facts=facts,
            )
        pm = re.search(r"\b(below|under|above|over)\s*" + CURRENCY + NUM, low)
        if pm:
            op = "LTE" if pm.group(1) in ("below", "under") else "GTE"
            return Parsed(
                kind="mandate",
                mandate=MandateIntent(kind="ALERT", instrument_ref=sym, op=op, threshold_price=_num(pm.group(2))),
                facts=facts,
            )
        return Parsed(kind="clarify", question="At what price or percentage drop should I alert you?", facts=facts)
    cond = re.search(r"\b(falls?|drops?|goes|is|trades)\s*(?:below|under|to|beneath)\s*" + CURRENCY + NUM, low) or re.search(
        r"\b(?:below|under)\s*" + CURRENCY + NUM, low
    )
    op = "LTE"
    if cond is None:
        cond = re.search(r"\b(rises?|goes|crosses|reaches|hits|is)\s*(?:above|over|to)?\s*" + CURRENCY + NUM, low)
        op = "GTE"
    if cond is None:
        return Parsed(kind="clarify", question="What trigger price should this instruction use? For example 'if price falls below 900'.", facts=facts)
    threshold = _num(cond.groups()[-1])
    action_part = re.split(r"\bif\b|\bwhen\b|\bonce\b", t, flags=re.I)[0]
    af = extract_facts(action_part)
    side = Side.BUY if "BUY" in af.sides else Side.SELL if "SELL" in af.sides else None
    if side is None or af.quantity is None:
        return Parsed(kind="clarify", question="Please state the exact side and quantity for this standing instruction, for example 'buy 100 TATAMOTORS if price falls below 900'.", facts=facts)
    limit = af.price if af.price is not None else threshold
    order = OrderIntent(action="PLACE", side=side, instrument_ref=sym, quantity=af.quantity, order_type=OrderType.LIMIT, limit_price=limit, product=af.product)
    return Parsed(
        kind="mandate",
        mandate=MandateIntent(kind="ORDER", instrument_ref=sym, op=op, threshold_price=threshold, order=order),
        facts=facts,
    )


def clauses(text: str) -> list[str]:
    return [c for c in re.split(r"\s*(?:,\s*)?\b(?:and then|then|and)\b\s*", text, flags=re.I) if ACTION_RE.search(c)]


def facts_for(text: str, instrument_ref: str | None) -> Facts:
    """Facts from the clause that mentions this instrument, so multi-action messages are checked clause by clause."""
    import dataclasses

    full = extract_facts(text)
    cl = clauses(text)
    if len(cl) <= 1 or not instrument_ref:
        return full
    ref = instrument_ref.upper()
    hits = [c for c in cl if ref in c.upper() or any(ref == normalize(t) for t in re.findall(r"[A-Za-z0-9&-]+", c))]
    if len(hits) == 1:
        f = extract_facts(hits[0])
        return f
    return dataclasses.replace(full, quantity=None, price=None, lots=None, trigger=None)


def normalize(tok: str) -> str:
    return re.sub(r"[^A-Z0-9&-]", "", tok.upper())
