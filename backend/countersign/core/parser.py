"""Deterministic interpreter for trader text.

Two jobs:
1. Misread guard: extract the facts the trader actually stated (side, quantity, price, symbols) so that any
   value proposed by the AI can be checked for provenance. A value the trader never stated is MODEL_INFERRED.
   An explicit fraction word ("half", "all", "25%", "a third") is a stated quantity rule: the quantity itself is
   then computed by the verifier from the trader's own holdings, never taken from the AI.
2. Offline engine: when no LLM is available (or it fails), route queries and build intents deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from countersign.core.schemas import MandateIntent, OrderIntent, OrderType, PlanIntent, Product, Side

NUM = r"(\d[\d,]*(?:\.\d+)?)"
CURRENCY = r"(?:₹|rs\.?|inr|rupees?)?\s*"
PCT = r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent|per\s?cent)"
SYMBOL_STOP = {
    "BUY", "SELL", "SHARES", "SHARE", "OF", "AT", "THE", "A", "AN", "MY", "AND", "THEN", "IF", "WHEN", "PRICE", "FALLS",
    "BELOW", "ABOVE", "RISES", "GOES", "LIMIT", "MARKET", "ORDER", "ORDERS", "CANCEL", "MODIFY", "CHANGE", "TO", "FOR",
    "QTY", "QUANTITY", "LOT", "LOTS", "INTRADAY", "DELIVERY", "CNC", "MIS", "NRML", "ME", "ALERT", "NOTIFY", "IT",
    "MORE", "THAN", "BY", "PERCENT", "USE", "PROCEEDS", "PLACE", "NEW", "WITH", "SOME", "ALL", "MY", "OPEN", "PENDING",
    "PLEASE", "NOW", "ON", "RS", "INR", "AROUND", "ABOUT", "ROUGHLY", "APPROX", "UNDER", "OVER", "IOC", "DAY", "ONLY",
    "ONCE", "REACHES", "HITS", "DROPS", "HOLDING", "HOLDINGS", "POSITION", "S", "X", "SL", "STOP", "LOSS", "TRIGGER",
    # fraction words, stop-loss and modify phrasing, alert and question words
    "HALF", "THIRD", "THIRDS", "QUARTER", "QUARTERS", "FOURTH", "FOURTHS", "ONE", "TWO", "THREE", "ENTIRE", "WHOLE",
    "EVERYTHING", "FULL", "PER", "CENT", "STOP-LOSS", "STOPLOSS", "SL-M", "SLM", "MOVE", "UP", "DOWN", "RAISE",
    "LOWER", "HIGHER", "TRAIL", "SHIFT", "ADJUST", "UPDATE", "SET", "POSITIONS", "STOCK", "STOCKS", "EXIT", "SQUARE",
    "OFF", "LOSING", "LOSERS", "ANY", "EACH", "EVERY", "TELL", "IN", "DROP", "FALL", "FALLS", "DIPS", "GOES", "IS",
    "WHAT", "DID", "I", "PAY", "PAID", "AVERAGE", "AVG", "COST", "PURCHASE", "BOUGHT", "WHICH", "ARE", "SHOW",
    "TODAY", "AS", "SM", "AGAIN", "INSTEAD", "FROM", "ITS", "MKT",
}
ROUGH_WORDS = re.compile(r"\b(around|about|roughly|approx(?:imately)?|near|some|few|couple|bunch|most|half)\b", re.I)

SELL_VERB = r"\b(?:sell|exit|square[\s-]?off|dump|offload|liquidate)\b"
FRACTION_WORDS: list[tuple[str, Fraction, str]] = [
    (r"(?:all|entire|whole|everything|full)", Fraction(1), "all"),
    (r"half", Fraction(1, 2), "half"),
    (r"(?:a|one)[\s-]third", Fraction(1, 3), "a third"),
    (r"two[\s-]thirds?", Fraction(2, 3), "two thirds"),
    (r"(?:a|one)[\s-](?:quarter|fourth)", Fraction(1, 4), "a quarter"),
    (r"three[\s-](?:quarters|fourths)", Fraction(3, 4), "three quarters"),
]
SL_RE = re.compile(r"\bstop[\s-]?loss(?:es)?\b|\bsl[\s-]?m\b|\bsl\b|\bstop[\s-]?(?:order|market|limit)\b|\btrigger\b", re.I)
SLM_RE = re.compile(r"\bsl[\s-]?m\b|\bstop[\s-]?loss[\s-]market\b|\bstop[\s-]?market\b|\b(?:stop[\s-]?loss|sl)\s+(?:at\s+)?market\b", re.I)
MODIFY_VERB = re.compile(r"\b(move|modify|change|update|raise|lower|trail|shift|adjust|bump|revise|amend|tighten|loosen|push|pull|set)\b", re.I)
MANDATE_COND = re.compile(
    r"\b(if|when|once|whenever)\b.*\b(falls?|drops?|dips?|slips?|declines?|rises?|goes|gets|reaches|hits|crosses|breaks|trades|moves)\b"
    r"|\b(if|when|once)\b.*\bis\s+(?:below|under|above|over)\b",
    re.I,
)


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
    fraction: Fraction | None = None
    fraction_text: str | None = None
    stop_loss: bool = False
    slm: bool = False
    direction: str | None = None  # UP | DOWN, for "move my stop-loss up to 1640"
    basis: str | None = None  # HOLDING | POSITION, when the trader says which one

    def mentions_number(self, value: Decimal | int | None) -> bool:
        if value is None:
            return False
        v = Decimal(str(value))
        return any(n == v for n in self.numbers)


@dataclass
class Parsed:
    kind: str  # query | order | modify | cancel | plan | mandate | exit_losers | rebalance | clarify | unknown
    query: str | None = None
    args: dict = field(default_factory=dict)
    order: OrderIntent | None = None
    plan: PlanIntent | None = None
    mandate: MandateIntent | None = None
    question: str | None = None
    options: list[str] = field(default_factory=list)
    facts: Facts | None = None


def fraction_str(fr: Fraction) -> str:
    return f"{fr.numerator}/{fr.denominator}"


def parse_fraction(raw: str | float | int | None) -> Fraction | None:
    """A fraction as an AI tool might pass it: '1/2', '0.5', '50%', 'half', 'all'. None if it is not one."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    for rx, fr, _ in FRACTION_WORDS:
        if re.fullmatch(rx, s):
            return fr
    try:
        if s.endswith("%"):
            fr = Fraction(Decimal(s[:-1].strip())) / 100
        elif "/" in s:
            a, b = s.split("/", 1)
            fr = Fraction(int(a), int(b))
        else:
            fr = Fraction(Decimal(s))
    except (ValueError, ZeroDivisionError, InvalidOperation):
        return None
    return fr if 0 < fr <= 1 else None


def _fraction(low: str) -> tuple[Fraction, str, tuple[int, int]] | None:
    """An explicit fraction of a holding, directly after a sell verb: 'sell half my TCS', 'sell 25% of INFY'."""
    lead = SELL_VERB + r"\s+(?:off\s+)?(?:(?:my|the)\s+)?"
    for rx, fr, label in FRACTION_WORDS:
        m = re.search(lead + r"(" + rx + r")\b", low)
        if m:
            return fr, label, m.span(1)
    m = re.search(lead + PCT, low)
    if m:
        pct = _num(m.group(1))
        if pct is not None and 0 < pct <= 100:
            fr = Fraction(pct) / 100
            return fr, f"{pct.normalize():f}%", m.span(1)
    return None


def _stop_loss_prices(low: str, modify: bool) -> tuple[Decimal | None, Decimal | None]:
    """(trigger, limit) for stop-loss phrasing. The trigger is never read as the limit price."""
    trig = re.search(r"\btrigger(?:\s+price)?\s*(?:at|of|@|=|to)?\s*" + CURRENCY + NUM, low)
    if trig is None:
        # "order" only before at/of: in "stop-loss order 777009" the number is an order id, not a trigger.
        trig = re.search(r"(?:\bstop[\s-]?loss|\bsl[\s-]?m|\bsl|\bstop)(?:\s+(?:market|limit))?(?:\s+order\s*(?:at|of|@|=)|\s*(?:at|of|@|=))?\s*" + CURRENCY + NUM, low)
    if trig is None and modify:
        tos = list(re.finditer(r"\bto\s*" + CURRENCY + NUM, low))
        trig = tos[-1] if tos else None
    lim = re.search(r"\blimit(?:\s+price)?\s*(?:at|of|@|=|to)?\s*" + CURRENCY + NUM, low)
    limit = _num(lim.group(1)) if lim else None
    if limit is None and not modify:
        for m in re.finditer(r"(?:\bat\b|@)\s*" + CURRENCY + NUM, low):
            if trig is None or m.start(1) != trig.start(1):
                limit = _num(m.group(1))
                break
    return (_num(trig.group(1)) if trig else None), limit


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
    m = re.search(r"\b(?:buy|sell|purchase)\s+(\d[\d,]*)\b(?!\s*(?:%|percent|per\s?cent))", low)
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
    if re.search(r"\bholdings?\b|\bdelivery\b|\bcnc\b", low):
        f.basis = "HOLDING"
    elif re.search(r"\bpositions?\b|\bintraday\b|\bmis\b|\bnrml\b", low):
        f.basis = "POSITION"
    fr = _fraction(low)
    rough_text = low
    if fr is not None:
        f.fraction, f.fraction_text, span = fr
        rough_text = low[: span[0]] + " " + low[span[1] :]
    f.rough = bool(ROUGH_WORDS.search(rough_text))
    is_modify = bool(MODIFY_VERB.search(low)) and not re.search(r"\b(?:buy|sell|purchase)\s+\d", low)
    if SL_RE.search(low) or (is_modify and re.search(r"\bstop\b", low)):
        f.stop_loss = True
        f.slm = bool(SLM_RE.search(low))
        f.trigger, f.price = _stop_loss_prices(low, is_modify)
        if f.slm:
            f.market = False
    up = re.search(r"\b(up|higher|raise|increase)\b", low)
    down = re.search(r"\b(down|lower|reduce|decrease)\b", low)
    if up and not down:
        f.direction = "UP"
    elif down and not up:
        f.direction = "DOWN"
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


# ------------------------------------------------------------------ request detectors (shared with the AI tool guards)


def detect_holdings_alert(text: str) -> dict | None:
    """'Tell me when any holding falls 3% in a day': an alert over every holding, never an order."""
    low = text.lower()
    if not re.search(r"\b(alert|notify|tell|warn|ping|let)\s+me\b|\balert\b", low):
        return None
    if not re.search(r"\b(?:any|each|every|all|one)\s+(?:of\s+)?(?:my\s+)?(?:holdings?|stocks?|shares?|scrips?)\b", low):
        return None
    if not re.search(r"\b(falls?|drops?|dips?|down|declines?|loses?|slips?|crash(?:es)?|tanks?)\b", low):
        return None
    pct = re.search(PCT, low)
    basis = None
    if re.search(r"\bin (?:a|one|a single) day\b|\btoday\b|\bintraday\b|\bdaily\b|\bday\b|\bsession\b|\bprevious close\b|\byesterday'?s? close\b", low):
        basis = "DAY"
    elif re.search(r"\b(average|avg|buy price|purchase price|cost|what i paid|bought)\b", low):
        basis = "AVG"
    return {"pct": _num(pct.group(1)) if pct else None, "basis": basis}


def detect_exit_losers(text: str) -> dict | None:
    """'Exit all my losing intraday positions'."""
    low = text.lower()
    if not re.search(r"\b(exit|close|square[\s-]?off|sell|get out of|cut|dump|liquidate)\b", low):
        return None
    if not re.search(r"\blos(?:ing|ers?|s[\s-]making)\b|\bin (?:the )?(?:red|loss)\b|\bunderwater\b", low):
        return None
    if not re.search(r"\bpositions?\b|\bintraday\b|\bmis\b|\btrades?\b", low):
        return None
    return {"intraday_only": bool(re.search(r"\bintraday\b|\bmis\b|\bday trades?\b", low))}


def detect_rebalance(text: str) -> dict | None:
    """'Rebalance so no single stock is more than 20% of my portfolio'."""
    low = text.lower()
    hit = (
        re.search(r"\brebalanc", low)
        or re.search(r"\bno (?:single |one )?(?:stock|holding|share|scrip|name)\b.*\b(?:more than|above|over|exceeds?|bigger than|greater than)\b", low)
        or re.search(r"\bcap (?:each|every|any|all|my) (?:stock|holding)s?\b", low)
    )
    if not hit:
        return None
    pct = re.search(PCT, low)
    return {"max_pct": _num(pct.group(1)) if pct else None}


def detect_modify_stop_loss(text: str) -> bool:
    low = text.lower()
    if not MODIFY_VERB.search(low) or re.search(r"\b(?:buy|sell|purchase)\s+\d", low):
        return False
    return bool(re.search(r"\bstop[\s-]?loss(?:es)?\b|\bsl[\s-]?m\b|\bsl\b|\bstop\b|\btrigger\b", low))


QUERY_RULES: list[tuple[str, re.Pattern]] = [
    ("rejected_orders", re.compile(r"\brejected\b.*\border|order.*\brejected\b", re.I)),
    ("open_orders", re.compile(r"\b(open|pending|outstanding)\s+orders?\b|\bmy orders\b|\border book\b", re.I)),
    ("losers", re.compile(r"\b(down|fallen|lost|losing|loss)\b.*\b(\d+(?:\.\d+)?)\s*(%|percent)|\blosers?\b|\bin the red\b", re.I)),
    (
        "avg_price",
        re.compile(
            r"\baverage\b.*\b(price|cost|paid|pay)\b|\b(paid|pay)\b.*\baverage\b|\bavg\b|\bwhat did i (?:pay|buy)\b|\bhow much did i pay\b"
            r"|\bbought\b.*\bat what\b|\b(?:buy|purchase|cost) price\b",
            re.I,
        ),
    ),
    ("option_chain", re.compile(r"\boption(s)?\b.*\b(chain|money|atm|nifty)|\bnifty\b.*\boptions?\b|\bnear the money\b|\bat the money\b|\batm\b", re.I)),
    ("margin", re.compile(r"\b(margin|funds|cash|buying power|available balance)\b", re.I)),
    ("exposure", re.compile(r"\bexposure\b", re.I)),
    ("pnl", re.compile(r"\bp\s*&\s*l\b|\bpnl\b|\bprofit\b|\bhow am i doing\b|\bmy (gains|returns)\b", re.I)),
    ("positions", re.compile(r"\bpositions?\b", re.I)),
    ("holdings", re.compile(r"\bholdings?\b|\bportfolio\b|\bwhat do i (own|hold)\b", re.I)),
    ("quote", re.compile(r"\b(price|quote|ltp|trading at|how is)\b", re.I)),
]

ACTION_RE = re.compile(r"\b(buy|sell|purchase|cancel|modify|change|alert|notify)\b", re.I)


def _query_args(name: str, low: str, facts: Facts) -> dict:
    args: dict = {}
    if name == "losers":
        pct = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", low)
        args["threshold_pct"] = float(pct.group(1)) if pct else 5.0
        holdings_only = re.search(r"\bholdings?\b|\bstocks?\b|\bportfolio\b", low) and not re.search(r"\bpositions?\b", low)
        args["scope"] = "holdings" if holdings_only else "both"
        args["basis"] = "day" if re.search(r"\btoday\b|\bin a day\b|\bday\b|\bsession\b", low) else "avg"
    if name in ("avg_price", "quote"):
        args["symbols"] = facts.symbols
    if name == "option_chain":
        args["width"] = 6
    return args


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
    if (ha := detect_holdings_alert(t)) is not None:
        return _parse_holdings_alert(t, ha, facts)
    if re.search(r"\b(alert|notify|tell)\s+me\b", low) or MANDATE_COND.search(low):
        return _parse_mandate(t, facts)
    if (ex := detect_exit_losers(t)) is not None:
        return Parsed(kind="exit_losers", args=ex, facts=facts)
    if (rb := detect_rebalance(t)) is not None:
        if rb["max_pct"] is None:
            return Parsed(kind="clarify", question="What is the maximum weight for a single stock? For example 'Rebalance so no single stock is more than 20% of my portfolio'.", facts=facts)
        return Parsed(kind="rebalance", args=rb, facts=facts)
    if detect_modify_stop_loss(t):
        return _parse_modify_stop_loss(t, facts)

    is_question = bool(re.match(r"^\s*(what|which|show|how|list|tell|give|display|any|do i|am i|where|get)\b", low)) or low.rstrip().endswith("?")
    if is_question and not re.search(r"\b(buy|sell|purchase)\s+\d", low):
        for name, rx in QUERY_RULES:
            if rx.search(t):
                return Parsed(kind="query", query=name, args=_query_args(name, low, facts), facts=facts)

    parts = re.split(r"\s*(?:,\s*)?\b(?:and then|then|and)\b\s*", t, flags=re.I)
    action_parts = [p for p in parts if ACTION_RE.search(p)]
    if len(action_parts) >= 2 and any(re.search(r"\b(buy|sell|cancel|place)\b", p, re.I) for p in action_parts):
        return _parse_plan(t, action_parts, facts)

    if re.search(r"\bcancel\b", low):
        return _parse_cancel(t, facts)
    if re.search(r"\b(modify|change|update|move)\b.*\border\b|\border\b.*\b(to|price)\b.*\d", low) and not re.search(r"\b(buy|sell)\b", low):
        return _parse_modify(t, facts)
    if re.search(r"\b(buy|sell|purchase|exit|square off)\b", low):
        return _parse_order(t, facts)

    for name, rx in QUERY_RULES:
        if rx.search(t):
            return Parsed(kind="query", query=name, args=_query_args(name, low, facts), facts=facts)
    return Parsed(kind="unknown", facts=facts)


def _fmt(d: Decimal | None) -> str:
    return f"₹{d:,}" if d is not None else "-"


def _order_from_text(t: str, facts: Facts) -> tuple[OrderIntent | None, str | None, list[str]]:
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
    fraction = None
    if facts.fraction is not None and qty is None and not facts.lots:
        if side != Side.SELL:
            return None, f"I can work out a fraction only of something you hold, when selling. How many shares of {sym.upper()} do you want to buy?", []
        fraction = facts.fraction
    if facts.lots:
        qty = None
    if facts.rough:
        return None, "Your request uses an approximate word (like 'some' or 'around'). Please state the exact quantity and price.", []
    if qty is None and not facts.lots and fraction is None:
        return None, f"How many shares of {sym.upper()} do you want to {side.value.lower()}? I will not guess a quantity.", []
    base = t.strip().rstrip(".?! ")
    price = None
    trigger = None
    if facts.stop_loss:
        if facts.trigger is None:
            return (
                None,
                "What trigger price for this stop-loss? For example 'sell 10 INFY stop loss trigger 1800 limit 1795', or 'sell 10 INFY SL-M trigger 1800' for a price-protected market order once triggered.",
                [],
            )
        trigger = facts.trigger
        if facts.slm:
            otype = OrderType.SLM
        elif facts.price is not None:
            otype = OrderType.SL
            price = facts.price
        else:
            return (
                None,
                f"Your stop-loss trigger is {_fmt(trigger)} but you gave no limit price. Should it be SL-M (a price-protected market order once triggered), or an SL order with a limit price you state (for example '... trigger {trigger} limit <price>')?",
                [f"{base} as SL-M"],
            )
    elif facts.market:
        otype = OrderType.MARKET
    elif facts.price is not None:
        otype = OrderType.LIMIT
        price = facts.price
    else:
        return (
            None,
            "At what price? Give a limit price (for example 'at 2800') or say 'at market' for a price-protected market order.",
            [f"{base} at market"],
        )
    intent = OrderIntent(
        action="PLACE",
        side=side,
        instrument_ref=sym,
        quantity=qty,
        order_type=otype,
        limit_price=price,
        trigger_price=trigger,
        product=facts.product,
        quantity_fraction=fraction_str(fraction) if fraction is not None else None,
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


def stop_loss_target(t: str, facts: Facts) -> tuple[str | None, str | None]:
    """(order id, instrument phrase) named in a stop-loss modification, e.g. 'on HDFC Bank' or 'order 256159'."""
    low = t.lower()
    oid = re.search(r"\border\s*(?:id\s*|no\.?\s*|number\s*|#\s*)?(\d{4,})\b", low) or re.search(r"#\s*(\d{4,})\b", low) or re.search(r"\b(\d{6,})\b", low)
    phrase = None
    m = re.search(r"\b(?:on|for|in|of)\s+(?:my\s+|the\s+)?(.+?)\s+(?:(?:up|down|higher|lower)\s+)?(?:to|from|at|trigger|by|limit)\b", t, re.I)
    if m:
        words = [w for w in re.findall(r"[^\s,;:!?()'\"]+", m.group(1)) if w.upper().strip(".") not in SYMBOL_STOP and not re.fullmatch(r"[\d,.%₹]+", w)]
        phrase = " ".join(words) or None
    if phrase is None:
        words = [w for w in facts.symbols if not re.fullmatch(r"\d+", w)]
        phrase = " ".join(words) or None
    return (oid.group(1) if oid else None), phrase


def _parse_modify_stop_loss(t: str, facts: Facts) -> Parsed:
    oid, phrase = stop_loss_target(t, facts)
    if facts.trigger is None:
        return Parsed(kind="clarify", question="What new trigger price for the stop-loss? For example 'move my stop-loss on HDFC Bank up to 1640'.", facts=facts)
    if oid is None and phrase is None:
        return Parsed(kind="clarify", question="Which stop-loss order? Name the stock (for example 'HDFC Bank') or give the order id.", facts=facts)
    return Parsed(
        kind="modify",
        order=OrderIntent(action="MODIFY", target_order_ref=oid, instrument_ref=phrase, trigger_price=facts.trigger, limit_price=facts.price),
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
            return Parsed(kind="clarify", question=f"For the step '{p.strip()}': {q}", options=[], facts=facts)
        steps.append(intent)
    return Parsed(kind="plan", plan=PlanIntent(steps=steps), facts=facts)


def _parse_holdings_alert(t: str, ha: dict, facts: Facts) -> Parsed:
    pct = ha["pct"]
    base = t.strip().rstrip(".?! ")
    if pct is None:
        return Parsed(kind="clarify", question="By how much should a holding fall before I alert you? For example 'tell me when any holding falls 3% in a day'.", facts=facts)
    if ha["basis"] is None:
        return Parsed(
            kind="clarify",
            question=f"Falls {pct.normalize():f}% measured how: from yesterday's close (in a day), or from your average buy price?",
            options=[f"{base} in a day", f"{base} below my average price"],
            facts=facts,
        )
    field_ = "DAY_CHANGE_BPS" if ha["basis"] == "DAY" else "DROP_FROM_AVG_BPS"
    return Parsed(
        kind="mandate",
        mandate=MandateIntent(kind="ALERT", scope="ALL_HOLDINGS", field=field_, op="LTE" if field_ == "DAY_CHANGE_BPS" else "GTE", drop_pct=pct, expires_days=30),
        facts=facts,
    )


def _mandate_parts(t: str) -> tuple[str, str]:
    """(action part, condition part) for 'buy 10 INFY if it falls below 1800' and 'If Reliance drops below 2800, buy 10'."""
    verb = re.search(r"\b(buy|sell|purchase)\b", t, re.I)
    if verb is None:
        return "", t
    start = verb.start()
    kw = re.search(r"\b(if|when|once|whenever)\b", t[start:], re.I)
    end = start + kw.start() if kw else len(t)
    return t[start:end], t[:start] + " " + t[end:]


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
            question="Which instrument should this standing instruction watch? Name the exact symbol (for example 'alert me if INFY falls more than 5%').",
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
    action_part, cond_part = _mandate_parts(t)
    cl = cond_part.lower()
    cond = re.search(r"\b(falls?|drops?|dips?|slips?|declines?|goes|gets|is|trades|moves|breaks|crosses)\s*(?:below|under|to|beneath|down to)\s*" + CURRENCY + NUM, cl) or re.search(
        r"\b(?:below|under)\s*" + CURRENCY + NUM, cl
    )
    op = "LTE"
    if cond is None:
        cond = re.search(r"\b(rises?|goes|gets|crosses|reaches|hits|is|trades|moves|breaks)\s*(?:above|over|to|past)?\s*" + CURRENCY + NUM, cl)
        op = "GTE"
    if cond is None:
        return Parsed(kind="clarify", question="What trigger price should this instruction use? For example 'if price falls below 900'.", facts=facts)
    threshold = _num(cond.groups()[-1])
    af = extract_facts(action_part)
    side = Side.BUY if "BUY" in af.sides else Side.SELL if "SELL" in af.sides else None
    if side is None or af.quantity is None:
        return Parsed(kind="clarify", question="Please state the exact side and quantity for this standing instruction, for example 'If RELIANCE drops below 1150, buy 10'.", facts=facts)
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
    return dataclasses.replace(full, quantity=None, price=None, lots=None, trigger=None, fraction=None, fraction_text=None)


def normalize(tok: str) -> str:
    return re.sub(r"[^A-Z0-9&-]", "", tok.upper())
