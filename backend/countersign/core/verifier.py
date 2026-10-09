"""Deterministic builder and verifier. Independent of any model.

build_*: turns an untrusted intent into a canonical object, attaching field provenance and running the misread
guard against the facts the trader actually stated.
verify_*: validates the canonical object (lot, tick, freeze, price band, margin, holdings, instrument status,
session, data freshness, plan consistency, mandate scope) and computes the wallet-style preview.
A BLOCK issue means no approval can be created and the broker is never called."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from countersign.core import charges, instruments, market, portfolio
from countersign.core.config import settings
from countersign.core.models import Instrument
from countersign.core.money import inr, round_to_tick, rupees_to_paise
from countersign.core.parser import Facts, detect_holdings_alert, normalize, parse_fraction
from countersign.core.schemas import (
    ALL_HOLDINGS_ID,
    CanonicalMandate,
    CanonicalOrder,
    CanonicalPlan,
    Disagreement,
    FieldRow,
    InstrumentHandle,
    Issue,
    MandateIntent,
    MandatePredicate,
    OrderIntent,
    OrderPreview,
    OrderStateRef,
    OrderType,
    PlanStepSpec,
    Product,
    Side,
    Source,
    Validity,
)

STOP_TYPES = (OrderType.SL, OrderType.SLM)
MAX_PLAN_STEPS = 6  # free-form plans the trader dictates step by step
MAX_GENERATED_PLAN_STEPS = 20  # exit and rebalance plans, where every step is computed by this verifier


def product_of(row: dict) -> str:
    """Broker product names normalised to ours (021 reports intraday as INTRADAY)."""
    p = str(row.get("product") or "").upper()
    return "MIS" if p == "INTRADAY" else p


# ------------------------------------------------------------------ account state


@dataclass
class AccountState:
    account_id: str
    cash_paise: int = 0
    available_paise: int = 0
    used_margin_paise: int = 0
    holdings: dict[str, dict] = field(default_factory=dict)
    positions: dict[str, dict] = field(default_factory=dict)
    position_rows: list[dict] = field(default_factory=list)  # one row per instrument AND product
    open_orders: list[dict] = field(default_factory=list)
    segments: list[str] = field(default_factory=lambda: ["EQ", "FO"])
    trading_enabled: bool = True

    @staticmethod
    def load(gateway, account_id: str) -> AccountState:
        funds = gateway.funds()
        st = AccountState(
            account_id=account_id,
            cash_paise=funds["cash_paise"],
            available_paise=funds["available_paise"],
            used_margin_paise=funds["used_margin_paise"],
        )
        for h in gateway.holdings():
            st.holdings[h["instrument_id"]] = h
        for p in gateway.positions():
            st.positions[p["instrument_id"]] = p
            st.position_rows.append(p)
        st.open_orders = gateway.orders(status="open")
        return st

    def holding_qty(self, instrument_id: str) -> int:
        h = self.holdings.get(instrument_id)
        return int(h["quantity"]) if h else 0

    def position_qty(self, instrument_id: str) -> int:
        p = self.positions.get(instrument_id)
        return int(p["quantity"]) if p else 0

    def pending_sell_qty(self, instrument_id: str, exclude_order_id: str | None = None) -> int:
        return sum(
            int(o["pending_quantity"])
            for o in self.open_orders
            if o["instrument_id"] == instrument_id
            and o["side"] == "SELL"
            and product_of(o) == "CNC"
            and (exclude_order_id is None or str(o["broker_order_id"]) != str(exclude_order_id))
        )

    def sellable_holding(self, instrument_id: str, exclude_order_id: str | None = None) -> tuple[int, str]:
        """Delivery quantity that can still be sold, and the arithmetic behind it.

        With 021 holdings as the gateway reports them (free_quantity = freeQty, settled; btst_quantity = btstQty,
        bought but not yet settled; today_quantity = today's net delivery buys from 021's positions; sell_quantity =
        sellQty, already sold or in pending sell orders today): free + btst + today - max(sellQty, pending CNC sells
        we can see). Selling a same-day delivery buy is an ordinary square-off on NSE, so it counts; 021's own risk
        checks still decide. With paper holdings (quantity only; fills already reduce it): quantity - pending CNC
        sells. A MODIFY excludes the order being modified (not an extra sale)."""
        h = self.holdings.get(instrument_id)
        if not h or int(h.get("quantity") or 0) <= 0:
            return 0, "no delivery holding"
        total = int(h["quantity"])
        pending = self.pending_sell_qty(instrument_id, exclude_order_id)
        own = self.pending_sell_qty(instrument_id) - pending
        if h.get("sell_quantity") is not None:
            t1 = int(h.get("btst_quantity") or 0)
            today = max(int(h.get("today_quantity") or 0), 0)
            free = int(h["free_quantity"]) if h.get("free_quantity") is not None else total - t1 - today
            used = max(max(int(h["sell_quantity"]) - own, 0), pending)
            sellable = max(free + t1 + today - used, 0)
            return sellable, f"{free:,} settled + {t1:,} unsettled (T1) + {today:,} bought today - {used:,} already sold or in pending sell orders = {sellable:,} sellable"
        sellable = max(total - pending, 0)
        if pending:
            return sellable, f"{total:,} held - {pending:,} in pending sell orders = {sellable:,} sellable"
        return sellable, f"{total:,} held, all sellable"

    def exposure_paise(self) -> int:
        tot = 0
        for h in self.holdings.values():
            tot += int(h["quantity"]) * int(h.get("ltp_paise") or h["avg_price_paise"])
        for p in self.positions.values():
            tot += abs(int(p["quantity"])) * int(p.get("ltp_paise") or p["avg_price_paise"])
        return tot


@dataclass
class BuildResult:
    canonical: CanonicalOrder | None = None
    fields: list[FieldRow] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    clarify: tuple[str, list[str]] | None = None
    instrument: Instrument | None = None


def _blocked(issues: list[Issue]) -> bool:
    return any(i.severity == "BLOCK" for i in issues)


def _trader_mentions_instrument(facts: Facts, inst: Instrument) -> bool:
    targets = {inst.symbol.upper(), *(a.upper() for a in (inst.aliases or []))}
    text_up = facts.text.upper()
    for t in targets:
        if t and re.search(r"(?<![A-Z0-9])" + re.escape(t) + r"(?![A-Z0-9])", text_up):
            return True
    if inst.segment == "FO" and inst.strike_paise:
        strike = str(inst.strike_paise // 100)
        return "NIFTY" in text_up and strike in text_up and (inst.option_type or "") in text_up.replace("CALL", "CE").replace("PUT", "PE")
    return False


def _resolve_option(s: Session, ref: str) -> Instrument | None:
    _, strike, kind = ref.split(":")
    exp = instruments.nifty_expiries()[0]
    return s.get(Instrument, instruments.option_id(exp, int(strike), kind))


def _resolve_ref(s: Session, ref: str, known_ids: list[str] | None = None) -> instruments.Resolution:
    """Resolve a phrase such as 'HDFC Bank': the whole phrase, then the words joined, then each word, then the symbols
    of instruments the trader already has orders in. A look-alike result is returned as is (it always blocks)."""
    r = instruments.resolve(s, ref)
    if r.status in ("RESOLVED", "LOOKALIKE"):
        return r
    words = ref.split()
    for cand in (["".join(words)] if len(words) > 1 else []) + (words if len(words) > 1 else []):
        r2 = instruments.resolve(s, cand)
        if r2.status in ("RESOLVED", "LOOKALIKE"):
            return r2
    norm = normalize(ref)
    for iid in known_ids or []:
        inst = s.get(Instrument, iid)
        if inst is not None and normalize(inst.symbol) == norm:
            return instruments._check_lookalike(inst)
    return r


def protection_price(side: Side, reference_paise: int, tick_size_paise: int) -> int:
    """Worst acceptable price for a market (or SL-M) order: the reference moved against the trader by the
    configured protection, rounded to the tick away from the trader (never silently better than stated)."""
    bps = settings().market_protection_bps
    raw = reference_paise * (10000 + bps) // 10000 if side == Side.BUY else reference_paise * (10000 - bps) // 10000
    return round_to_tick(raw, tick_size_paise, "up" if side == Side.BUY else "down")


def _frac_text(fr: Fraction) -> str:
    if fr.denominator == 1:
        return f"{fr.numerator:,}"
    return f"{float(fr):,.4f}".rstrip("0").rstrip(".")


@dataclass
class ComputedQty:
    quantity: int = 0
    product: Product | None = None
    arithmetic: str = ""
    basis: str = ""
    message: str | None = None  # set when no order can be drafted; explains why
    options: list[str] = field(default_factory=list)


def fraction_quantity(account: AccountState, inst: Instrument, frac: Fraction, label: str, facts: Facts, product_hint: Product | None) -> ComputedQty:
    """Quantity for 'sell half my TCS': the stated fraction of what the trader can sell, rounded DOWN to the lot."""
    sym = inst.symbol
    unit = "units" if inst.segment == "FO" else "shares"
    held, held_note = account.sellable_holding(inst.instrument_id)
    has_holding = account.holding_qty(inst.instrument_id) > 0
    rows = [p for p in account.position_rows if p["instrument_id"] == inst.instrument_id and product_of(p) in ("MIS", "NRML") and int(p["quantity"]) != 0]
    if product_hint in (Product.MIS, Product.NRML):
        rows = [p for p in rows if product_of(p) == product_hint.value]
    want_holding = facts.basis == "HOLDING" or product_hint == Product.CNC
    want_position = facts.basis == "POSITION" or product_hint in (Product.MIS, Product.NRML)
    base_text = facts.text.strip().rstrip(".?! ")
    if want_holding and not want_position:
        source = "holding"
    elif want_position and not want_holding:
        source = "position"
    elif has_holding and rows:
        return ComputedQty(
            message=f"You have both a delivery holding of {sym} ({held_note}) and an open {product_of(rows[0])} position. Which one should I sell {label} of?",
            options=[f"{base_text} from my delivery holding", f"{base_text} from my {'intraday' if product_of(rows[0]) == 'MIS' else 'NRML'} position"],
        )
    elif has_holding:
        source = "holding"
    elif rows:
        source = "position"
    else:
        return ComputedQty(message=f"You have no {sym} to sell: no delivery holding and no open position. No order was drafted.")
    if source == "holding":
        if held <= 0:
            return ComputedQty(message=f"You have no sellable {sym} in delivery ({held_note}). No order was drafted.")
        base, product, basis = held, Product.CNC, f"Your {sym} delivery holding: {held_note}"
    else:
        if not rows:
            return ComputedQty(message=f"You have no open {sym} position to sell from. No order was drafted.")
        if len(rows) > 1:
            return ComputedQty(message=f"You have {len(rows)} open {sym} positions ({', '.join(product_of(p) for p in rows)}). Say which product to sell from.")
        p = rows[0]
        q = int(p["quantity"])
        if q < 0:
            return ComputedQty(message=f"Your {sym} {product_of(p)} position is short {abs(q):,}; selling would add to the short. To close it, buy to cover. No order was drafted.")
        base, product, basis = q, Product(product_of(p)), f"Your {sym} {product_of(p)} position: long {q:,}"
    raw = Fraction(base) * frac
    whole = math.floor(raw)
    lot = max(int(inst.lot_size or 1), 1)
    qty = whole - whole % lot
    arithmetic = f"{label} of {base:,} {unit} = {_frac_text(raw)}"
    if lot > 1 and qty != raw:
        arithmetic += f", rounded down to whole lots of {lot}: {qty:,}"
    elif qty != raw:
        arithmetic += f", rounded down to {qty:,} whole {unit}"
    if qty <= 0:
        what = f"whole lots of {lot}" if lot > 1 else f"whole {unit}"
        return ComputedQty(message=f"{label[:1].upper() + label[1:]} of your {base:,} {sym} {unit} is {_frac_text(raw)}, which rounds down to 0 {what}, so there is nothing to sell. No order was drafted.")
    return ComputedQty(quantity=qty, product=product, arithmetic=arithmetic, basis=basis)


# ------------------------------------------------------------------ build


def build_order(
    s: Session,
    intent: OrderIntent,
    facts: Facts,
    account: AccountState,
    *,
    engine: str,
    replace_source: dict | None = None,
) -> BuildResult:
    res = BuildResult()
    issues = res.issues
    is_model = engine != "deterministic"

    if intent.action in ("MODIFY", "CANCEL"):
        return _build_modify_cancel(s, intent, facts, account, engine)

    # --- instrument
    inst: Instrument | None = None
    if replace_source is not None:
        inst = s.get(Instrument, replace_source["instrument_id"])
    elif intent.instrument_ref and intent.instrument_ref.startswith("NIFTYOPT:"):
        inst = _resolve_option(s, intent.instrument_ref)
        if inst is None:
            res.clarify = ("That NIFTY strike is not listed for the nearest expiry. Ask me for the option chain to see listed strikes.", [])
            return res
    else:
        r = instruments.resolve(s, intent.instrument_ref or "")
        if r.status == "LOOKALIKE":
            issues.append(Issue(code="LOOKALIKE_SYMBOL", severity="BLOCK", message=r.message, field="instrument"))
            inst = r.instrument
            if inst is None:
                res.clarify = (r.message + " Please type the exact ASCII symbol.", [c.symbol for c in r.candidates if instruments.is_ascii_symbol(c.symbol)])
                return res
        elif r.status == "AMBIGUOUS":
            res.clarify = (r.message + " Which one did you mean?", [c.symbol for c in r.candidates][:6])
            return res
        elif r.status == "NOT_FOUND":
            res.clarify = (r.message + " Please give the exact NSE trading symbol.", [])
            return res
        else:
            inst = r.instrument
    res.instrument = inst
    assert inst is not None

    inst_source = Source.TRADER
    inst_note = None
    if replace_source is not None:
        inst_source, inst_note = Source.ACCOUNT, "Copied from the order you asked me to replace."
    elif not _trader_mentions_instrument(facts, inst):
        inst_source = Source.MODEL
        inst_note = "You did not name this instrument. The AI chose it."
        issues.append(Issue(code="INSTRUMENT_NOT_STATED", severity="BLOCK", message=f"The instrument {inst.symbol} was not named in your request; the AI inferred it. Refusing to trade an instrument you did not specify.", field="instrument"))
    if "UNTRUSTED_TEXT_REMOVED" in (inst.flags or []):
        issues.append(Issue(code="UNTRUSTED_TEXT_REMOVED", severity="INFO", message=f"The vendor description for {inst.symbol} contained instruction-like text. It was quarantined and never shown to the AI.", field="instrument"))

    # --- side
    side = intent.side
    if replace_source is not None and side is None:
        side = Side(replace_source["side"])
    side_source = Source.TRADER
    side_note = None
    if side is None:
        res.clarify = ("Do you want to buy or sell?", ["Buy", "Sell"])
        return res
    if replace_source is not None and intent.side is None:
        side_source, side_note = Source.ACCOUNT, "Copied from the cancelled order."
    elif side.value not in facts.sides:
        side_source, side_note = Source.MODEL, f"You did not say '{side.value.lower()}'."
        issues.append(Issue(code="SIDE_NOT_STATED", severity="BLOCK", message=f"The AI chose {side.value} but your request does not say {side.value.lower()}.", field="side"))
    if facts.sides and len(facts.sides) == 1 and side.value not in facts.sides and replace_source is None:
        res.disagreements.append(Disagreement(field="side", trader_value=next(iter(facts.sides)), model_value=side.value))

    # --- quantity
    qty_note = None
    qty_source = Source.TRADER
    computed: ComputedQty | None = None
    model_frac = parse_fraction(intent.quantity_fraction) if intent.quantity_fraction else None
    if intent.quantity_fraction and model_frac is None:
        res.clarify = (f"'{intent.quantity_fraction}' is not a fraction I can apply. Say for example 'sell half my {inst.symbol}' or 'sell 25% of my {inst.symbol}'.", [])
        return res
    if intent.target_order_ref and intent.target_order_ref.startswith("LOTS:"):
        lots = int(intent.target_order_ref.split(":")[1])
        qty = lots * inst.lot_size
        qty_note = f"{lots} lot(s) x lot size {inst.lot_size} (instrument metadata)"
    elif replace_source is not None and intent.quantity is None:
        qty = int(replace_source["quantity"])
        qty_source, qty_note = Source.ACCOUNT, "Copied from the cancelled order."
    elif side == Side.SELL and facts.fraction is not None and facts.quantity is None:
        # The trader stated a fraction ("half", "all", "25%"): the quantity is computed here from their own holding,
        # never taken from the AI. An AI quantity or fraction that differs is a misread and blocks.
        # Holding or position is decided by the trader's words (or by what exists), never by a product the AI picked.
        computed = fraction_quantity(account, inst, facts.fraction, facts.fraction_text or "", facts, facts.product)
        if computed.message:
            res.clarify = (computed.message, computed.options)
            return res
        qty = computed.quantity
        qty_source, qty_note = Source.ACCOUNT, f"Computed from your holdings: {computed.arithmetic}"
        if model_frac is not None and model_frac != facts.fraction:
            qty_source = Source.MODEL
            res.disagreements.append(Disagreement(field="quantity", trader_value=facts.fraction_text or "", model_value=intent.quantity_fraction or ""))
            issues.append(Issue(code="FRACTION_MISMATCH", severity="BLOCK", message=f"Misread guard: you said '{facts.fraction_text}' but the AI used the fraction {intent.quantity_fraction}.", field="quantity"))
        if intent.quantity is not None and int(intent.quantity) != qty:
            qty_source = Source.MODEL
            res.disagreements.append(Disagreement(field="quantity", trader_value=f"{facts.fraction_text} = {qty}", model_value=str(intent.quantity)))
            issues.append(Issue(code="QUANTITY_MISMATCH", severity="BLOCK", message=f"Misread guard: {computed.arithmetic}, but the AI proposed {intent.quantity}. Nothing was approved.", field="quantity"))
    elif model_frac is not None:
        computed = fraction_quantity(account, inst, model_frac, intent.quantity_fraction or "", facts, facts.product)
        if computed.message:
            res.clarify = (computed.message, computed.options)
            return res
        qty = computed.quantity
        qty_source, qty_note = Source.MODEL, f"You did not state a fraction; the AI chose {intent.quantity_fraction}."
        issues.append(Issue(code="FRACTION_NOT_STATED", severity="BLOCK", message=f"You did not ask to sell a fraction of {inst.symbol}; the AI chose {intent.quantity_fraction}. Refusing a quantity you never stated or implied.", field="quantity"))
    else:
        qty = intent.quantity
        if qty is None:
            res.clarify = (f"How many units of {inst.symbol} do you want to {side.value.lower()}? I will not guess a quantity.", [])
            return res
        if facts.quantity is not None and facts.quantity != qty:
            qty_source = Source.MODEL
            qty_note = f"AI proposed {qty} but you said {facts.quantity}."
            res.disagreements.append(Disagreement(field="quantity", trader_value=str(facts.quantity), model_value=str(qty)))
            issues.append(Issue(code="QUANTITY_MISMATCH", severity="BLOCK", message=f"Misread guard: you asked for {facts.quantity} but the AI proposed {qty}. Nothing was approved; please restate the quantity.", field="quantity"))
        elif not facts.mentions_number(qty):
            qty_source = Source.MODEL
            qty_note = "This quantity does not appear in your request."
            issues.append(Issue(code="QUANTITY_NOT_STATED", severity="BLOCK", message=f"The quantity {qty} was not stated by you; the AI inferred it.", field="quantity"))

    # --- order type and prices
    otype = intent.order_type or (OrderType.LIMIT if intent.limit_price is not None else None)
    if otype is None:
        res.clarify = ("Should this be a limit order (give a price) or a price-protected market order?", ["At market (price-protected)"])
        return res
    type_source = Source.TRADER
    if otype == OrderType.MARKET and not facts.market:
        type_source = Source.MODEL
        issues.append(Issue(code="ORDER_TYPE_NOT_STATED", severity="BLOCK", message="The AI chose a MARKET order but you did not ask for one.", field="order_type"))
    if otype == OrderType.LIMIT and facts.market and replace_source is None:
        res.disagreements.append(Disagreement(field="order_type", trader_value="MARKET", model_value="LIMIT"))
    if otype in STOP_TYPES and not facts.stop_loss:
        type_source = Source.MODEL
        issues.append(Issue(code="ORDER_TYPE_NOT_STATED", severity="BLOCK", message=f"The AI chose a {otype.value} (stop-loss) order but you did not ask for a stop-loss.", field="order_type"))
    if facts.stop_loss and replace_source is None:
        wanted = OrderType.SLM if facts.slm else OrderType.SL if facts.price is not None else None
        if otype not in STOP_TYPES or (wanted is not None and otype != wanted):
            type_source = Source.MODEL
            res.disagreements.append(Disagreement(field="order_type", trader_value=wanted.value if wanted else "stop-loss", model_value=otype.value))
            issues.append(Issue(code="ORDER_TYPE_MISMATCH", severity="BLOCK", message=f"Misread guard: you asked for a {wanted.value if wanted else 'stop-loss'} order but the AI drafted {otype.value}.", field="order_type"))
        elif wanted is None and otype == OrderType.SLM:
            type_source = Source.MODEL
            issues.append(Issue(code="ORDER_TYPE_NOT_STATED", severity="BLOCK", message="You gave a stop-loss trigger but did not say SL-M (market once triggered); the AI chose it. Say 'SL-M', or state a limit price.", field="order_type"))

    limit_paise = None
    price_source = Source.TRADER
    price_note = None
    if otype in (OrderType.LIMIT, OrderType.SL):
        if intent.limit_price is None:
            res.clarify = (f"What limit price for {inst.symbol}?", [])
            return res
        limit_paise = rupees_to_paise(intent.limit_price)
        if facts.price is not None and Decimal(str(facts.price)) != Decimal(str(intent.limit_price)):
            price_source = Source.MODEL
            price_note = f"AI proposed {inr(limit_paise)} but you said {inr(rupees_to_paise(facts.price))}."
            res.disagreements.append(Disagreement(field="limit_price", trader_value=str(facts.price), model_value=str(intent.limit_price)))
            issues.append(Issue(code="PRICE_MISMATCH", severity="BLOCK", message=f"Misread guard: you said {inr(rupees_to_paise(facts.price))} but the AI proposed {inr(limit_paise)}.", field="limit_price"))
        elif not facts.mentions_number(intent.limit_price):
            price_source = Source.MODEL
            price_note = "This price does not appear in your request."
            issues.append(Issue(code="PRICE_NOT_STATED", severity="BLOCK", message=f"The limit price {inr(limit_paise)} was not stated by you; the AI inferred it.", field="limit_price"))
        elif otype == OrderType.SL and facts.price is None and facts.trigger is not None and Decimal(str(facts.trigger)) == Decimal(str(intent.limit_price)):
            price_source = Source.MODEL
            price_note = "You gave this number as the trigger, not as a limit price."
            issues.append(Issue(code="PRICE_NOT_STATED", severity="BLOCK", message=f"You stated {inr(limit_paise)} as the stop-loss trigger, not as a limit price; the AI reused it as the limit. State a limit price, or ask for SL-M.", field="limit_price"))

    trigger_paise = rupees_to_paise(intent.trigger_price) if intent.trigger_price is not None else None
    trig_source = Source.TRADER
    trig_note = None
    if otype in STOP_TYPES and trigger_paise is None:
        res.clarify = (f"What trigger price for this {otype.value} order on {inst.symbol}?", [])
        return res
    if trigger_paise is not None:
        if facts.trigger is not None and Decimal(str(facts.trigger)) != Decimal(str(intent.trigger_price)):
            trig_source, trig_note = Source.MODEL, f"AI proposed {inr(trigger_paise)} but you said {inr(rupees_to_paise(facts.trigger))}."
            res.disagreements.append(Disagreement(field="trigger_price", trader_value=str(facts.trigger), model_value=str(intent.trigger_price)))
            issues.append(Issue(code="TRIGGER_MISMATCH", severity="BLOCK", message=f"Misread guard: you said trigger {inr(rupees_to_paise(facts.trigger))} but the AI proposed {inr(trigger_paise)}.", field="trigger_price"))
        elif facts.trigger is None and not facts.mentions_number(intent.trigger_price):
            trig_source, trig_note = Source.MODEL, "This trigger does not appear in your request."
            issues.append(Issue(code="TRIGGER_NOT_STATED", severity="BLOCK", message=f"The trigger price {inr(trigger_paise)} was not stated by you; the AI inferred it.", field="trigger_price"))

    tick = market.latest(inst.instrument_id)
    protection = None
    protection_note = None
    if otype == OrderType.MARKET:
        if tick is None:
            issues.append(Issue(code="NO_MARKET_DATA", severity="BLOCK", message=f"No live price for {inst.symbol}; a market order cannot be price-protected.", field="order_type"))
        else:
            protection = protection_price(side, tick.ltp_paise, inst.tick_size_paise)
            protection_note = f"Worst case: {inr(protection)} per unit, {inr(protection * int(qty))} for {int(qty):,}. The order fills at the best available price but never {'above' if side == Side.BUY else 'below'} this cap ({settings().market_protection_bps / 100:.2f}% from the live price {inr(tick.ltp_paise)})."
    elif otype == OrderType.SLM and trigger_paise is not None:
        # Once triggered, an SL-M order trades near the trigger, so its cap is measured from the trigger, not today's LTP.
        protection = protection_price(side, trigger_paise, inst.tick_size_paise)
        protection_note = f"Once triggered it fills at the best available price but never {'above' if side == Side.BUY else 'below'} {inr(protection)} ({settings().market_protection_bps / 100:.2f}% from the trigger)."

    product = intent.product
    product_source = Source.TRADER
    product_note = None
    if computed is not None and computed.product is not None:
        if product is None:
            product, product_source, product_note = computed.product, Source.ACCOUNT, "Same product as the holding or position being sold."
        elif product != computed.product:
            product_source = Source.MODEL
            issues.append(Issue(code="PRODUCT_MISMATCH", severity="BLOCK", message=f"The quantity was computed from your {computed.product.value} {'holding' if computed.product == Product.CNC else 'position'}, but the AI chose product {product.value}.", field="product"))
    elif product is None:
        product = Product.NRML if inst.segment == "FO" else Product.CNC
        product_source = Source.DEFAULT
    elif facts.product != product:
        product_source = Source.MODEL
        issues.append(Issue(code="PRODUCT_NOT_STATED", severity="WARN", message=f"Product {product.value} was chosen by the AI, not stated by you.", field="product"))
    validity = intent.validity or Validity.DAY

    canonical = CanonicalOrder(
        action="PLACE",
        account_id=account.account_id,
        instrument=instruments.handle(inst),
        side=side,
        quantity=int(qty),
        order_type=otype,
        product=product,
        validity=validity,
        limit_price_paise=limit_paise,
        trigger_price_paise=trigger_paise,
        protection_price_paise=protection,
    )
    res.canonical = canonical
    flagged_inst = inst_source == Source.MODEL or "LOOKALIKE_SYMBOL" in (inst.flags or [])
    res.fields = [
        FieldRow(key="side", label="Side", display=side.value, source=side_source, flagged=side_source == Source.MODEL, note=side_note),
        FieldRow(key="instrument", label="Instrument", display=f"{inst.symbol}  ({inst.display_name})", source=inst_source, flagged=flagged_inst, note=inst_note or ("Look-alike characters detected" if "LOOKALIKE_SYMBOL" in (inst.flags or []) else None)),
        FieldRow(key="instrument_id", label="Instrument ID", display=inst.instrument_id, source=Source.INSTRUMENT),
        FieldRow(key="quantity", label="Quantity", display=f"{qty:,}", source=qty_source, flagged=qty_source == Source.MODEL, note=qty_note),
    ]
    if computed is not None:
        res.fields.append(FieldRow(key="quantity_basis", label="Computed from", display=computed.basis, source=Source.ACCOUNT, note="Read from your broker account when this draft was made"))
    res.fields.append(FieldRow(key="order_type", label="Order type", display=otype.value, source=type_source, flagged=type_source == Source.MODEL))
    if trigger_paise is not None:
        res.fields.append(FieldRow(key="trigger_price", label="Trigger price", display=inr(trigger_paise), source=trig_source, flagged=trig_source == Source.MODEL, note=trig_note))
    if limit_paise is not None:
        res.fields.append(FieldRow(key="limit_price", label="Limit price", display=inr(limit_paise), source=price_source, flagged=price_source == Source.MODEL, note=price_note))
    if protection is not None:
        res.fields.append(FieldRow(key="protection_price", label="Worst-case protected price", display=inr(protection), source=Source.RISK, note=protection_note))
    res.fields += [
        FieldRow(key="product", label="Product", display=product.value, source=product_source, note=product_note),
        FieldRow(key="validity", label="Validity", display=validity.value, source=Source.DEFAULT if intent.validity is None else Source.TRADER),
        FieldRow(key="lot_size", label="Lot size", display=str(inst.lot_size), source=Source.INSTRUMENT),
        FieldRow(key="tick_size", label="Tick size", display=inr(inst.tick_size_paise), source=Source.INSTRUMENT),
    ]
    return res


def _state_ref(o: dict) -> OrderStateRef:
    return OrderStateRef(quantity=int(o["quantity"]), order_type=OrderType(o["order_type"]), limit_price_paise=o.get("limit_price_paise"), trigger_price_paise=o.get("trigger_price_paise"))


def _arrow(old: int | None, new: int | None) -> str:
    return f"{inr(old)} → {inr(new)}" if old != new else f"{inr(new)} (unchanged)"


def _build_modify_stop_loss(s: Session, intent: OrderIntent, facts: Facts, account: AccountState) -> BuildResult:
    """'Move my stop-loss on HDFC Bank up to 1640': find the trader's one open SL order on that instrument and build a
    MODIFY carrying its full new state (new trigger; for SL, a limit that keeps the original trigger-to-limit gap
    unless the trader stated one). Old values travel in modify_from so the card shows old -> new."""
    res = BuildResult()
    issues = res.issues
    named_by_id = False
    if intent.target_order_ref:
        target = next((o for o in account.open_orders if str(o["broker_order_id"]) == str(intent.target_order_ref)), None)
        if target is None:
            res.clarify = (f"I could not find an open order {intent.target_order_ref}. Ask me 'what are my open orders?' to see them.", [])
            return res
        if target.get("order_type") not in ("SL", "SL-M"):
            res.clarify = (f"Order {target['broker_order_id']} is a {target.get('order_type')} order, not a stop-loss, so it has no trigger to move.", [])
            return res
        named_by_id = True
        inst = s.get(Instrument, target["instrument_id"])
        if inst is None:
            res.clarify = (f"Order {target['broker_order_id']} is for an instrument that is not in today's instrument master.", [])
            return res
    else:
        r = _resolve_ref(s, intent.instrument_ref or "", [o["instrument_id"] for o in account.open_orders])
        if r.status == "LOOKALIKE":
            res.clarify = (r.message + " Please type the exact ASCII symbol.", [c.symbol for c in r.candidates if instruments.is_ascii_symbol(c.symbol)])
            return res
        if r.status != "RESOLVED":
            res.clarify = ((r.message or "Which instrument?") + " Which stock's stop-loss should I move? Give the exact symbol.", [c.symbol for c in r.candidates][:6])
            return res
        inst = r.instrument
        cands = [o for o in account.open_orders if o["instrument_id"] == inst.instrument_id and o.get("order_type") in ("SL", "SL-M")]
        new_text = f"{intent.trigger_price}" if intent.trigger_price is not None else "<new trigger>"
        if not cands:
            others = [o for o in account.open_orders if o["instrument_id"] == inst.instrument_id]
            msg = f"You have no open stop-loss order on {inst.symbol}, so there is no trigger to move."
            if others:
                msg += " Your open " + inst.symbol + " orders are: " + "; ".join(f"{o['broker_order_id']} ({o['side']} {int(o['quantity']):,} {o['order_type']})" for o in others) + "."
            msg += f" To place a new stop-loss, say for example 'sell 10 {inst.symbol} stop loss trigger <price> limit <price>'."
            res.clarify = (msg, [])
            return res
        if len(cands) > 1:
            listing = "; ".join(f"order {o['broker_order_id']}: {o['side']} {int(o['quantity']):,}, trigger {inr(o.get('trigger_price_paise'))}" for o in cands)
            res.clarify = (f"You have {len(cands)} open stop-loss orders on {inst.symbol} ({listing}). Which one should move?", [f"Move my stop-loss order {o['broker_order_id']} to {new_text}" for o in cands][:6])
            return res
        target = cands[0]
    res.instrument = inst

    if named_by_id:
        inst_source, inst_note = Source.ACCOUNT, "The instrument of the order you named"
        if str(target["broker_order_id"]) not in facts.text:
            issues.append(Issue(code="TARGET_NOT_STATED", severity="BLOCK", message=f"You did not name order {target['broker_order_id']}; the AI chose it.", field="target"))
            inst_source, inst_note = Source.MODEL, "You did not name this order. The AI chose it."
    elif _trader_mentions_instrument(facts, inst):
        inst_source, inst_note = Source.TRADER, None
    else:
        inst_source, inst_note = Source.MODEL, "You did not name this instrument. The AI chose it."
        issues.append(Issue(code="INSTRUMENT_NOT_STATED", severity="BLOCK", message=f"The instrument {inst.symbol} was not named in your request; the AI inferred it.", field="instrument"))

    otype = OrderType(target["order_type"])
    side = Side(target["side"])
    old_trig = target.get("trigger_price_paise")
    old_limit = target.get("limit_price_paise")
    if intent.trigger_price is None:
        res.clarify = (f"What new trigger price for your {inst.symbol} stop-loss (now {inr(old_trig)})?", [])
        return res
    new_trig = rupees_to_paise(intent.trigger_price)
    trig_source, trig_note = Source.TRADER, None
    if facts.trigger is not None and Decimal(str(facts.trigger)) != Decimal(str(intent.trigger_price)):
        trig_source, trig_note = Source.MODEL, f"AI proposed {inr(new_trig)} but you said {inr(rupees_to_paise(facts.trigger))}."
        res.disagreements.append(Disagreement(field="trigger_price", trader_value=str(facts.trigger), model_value=str(intent.trigger_price)))
        issues.append(Issue(code="TRIGGER_MISMATCH", severity="BLOCK", message=f"Misread guard: you said {inr(rupees_to_paise(facts.trigger))} but the AI proposed {inr(new_trig)}.", field="trigger_price"))
    elif facts.trigger is None and not facts.mentions_number(intent.trigger_price):
        trig_source, trig_note = Source.MODEL, "This trigger does not appear in your request."
        issues.append(Issue(code="TRIGGER_NOT_STATED", severity="BLOCK", message=f"The new trigger {inr(new_trig)} was not stated by you; the AI inferred it.", field="trigger_price"))
    if old_trig is not None and new_trig == old_trig:
        issues.append(Issue(code="NO_CHANGE", severity="BLOCK", message=f"The stop-loss trigger is already {inr(old_trig)}; there is nothing to modify.", field="trigger_price"))
    elif old_trig is not None and facts.direction is not None:
        wrong = (facts.direction == "UP" and new_trig < old_trig) or (facts.direction == "DOWN" and new_trig > old_trig)
        if wrong:
            res.disagreements.append(Disagreement(field="trigger_price", trader_value=f"move {facts.direction.lower()}", model_value=f"{inr(old_trig)} to {inr(new_trig)}"))
            issues.append(Issue(code="DIRECTION_MISMATCH", severity="BLOCK", message=f"You said to move the stop-loss {facts.direction.lower()}, but {inr(new_trig)} is {'below' if new_trig < old_trig else 'above'} the current trigger {inr(old_trig)}.", field="trigger_price"))

    new_limit = None
    limit_source, limit_note = Source.ACCOUNT, None
    if otype == OrderType.SL:
        if intent.limit_price is not None:
            new_limit = rupees_to_paise(intent.limit_price)
            limit_source = Source.TRADER
            if facts.price is not None and Decimal(str(facts.price)) != Decimal(str(intent.limit_price)):
                limit_source, limit_note = Source.MODEL, f"AI proposed {inr(new_limit)} but you said {inr(rupees_to_paise(facts.price))}."
                res.disagreements.append(Disagreement(field="limit_price", trader_value=str(facts.price), model_value=str(intent.limit_price)))
                issues.append(Issue(code="PRICE_MISMATCH", severity="BLOCK", message=f"Misread guard: you said limit {inr(rupees_to_paise(facts.price))} but the AI proposed {inr(new_limit)}.", field="limit_price"))
            elif facts.price is None:
                limit_source, limit_note = Source.MODEL, "You did not state a new limit price."
                issues.append(Issue(code="PRICE_NOT_STATED", severity="BLOCK", message=f"The new limit {inr(new_limit)} was not stated by you; the AI inferred it.", field="limit_price"))
        else:
            if old_limit is None or old_trig is None:
                res.clarify = (f"Your {inst.symbol} SL order has no current limit to carry over. What limit price should it use?", [])
                return res
            gap = old_limit - old_trig
            new_limit = new_trig + gap
            limit_note = f"Keeps your order's trigger-to-limit gap: old limit {inr(old_limit)} - old trigger {inr(old_trig)} = {inr(gap)}, so new limit = {inr(new_trig)} {'+' if gap >= 0 else '-'} {inr(abs(gap))}"
    elif intent.limit_price is not None:
        res.clarify = (f"Your {inst.symbol} stop-loss is SL-M (no limit price). I can move its trigger; changing it to an SL limit order is a new order. Cancel it and place a new SL order instead.", [])
        return res
    protection = protection_price(side, new_trig, inst.tick_size_paise) if otype == OrderType.SLM else None

    product = Product(product_of(target))
    canonical = CanonicalOrder(
        action="MODIFY",
        account_id=account.account_id,
        instrument=instruments.handle(inst),
        side=side,
        quantity=int(target["quantity"]),
        order_type=otype,
        product=product,
        validity=Validity(target.get("validity") or "DAY"),
        limit_price_paise=new_limit,
        trigger_price_paise=new_trig,
        protection_price_paise=protection,
        target_broker_order_id=str(target["broker_order_id"]),
        modify_from=_state_ref(target),
    )
    res.canonical = canonical
    res.fields = [
        FieldRow(key="action", label="Action", display="MODIFY stop-loss", source=Source.TRADER),
        FieldRow(key="target", label="Target order", display=str(target["broker_order_id"]), source=Source.ACCOUNT, note="Your only open stop-loss order on this instrument" if not named_by_id else "The order id you gave"),
        FieldRow(key="instrument", label="Instrument", display=f"{inst.symbol}  ({inst.display_name})", source=inst_source, flagged=inst_source == Source.MODEL, note=inst_note),
        FieldRow(key="instrument_id", label="Instrument ID", display=inst.instrument_id, source=Source.INSTRUMENT),
        FieldRow(key="side", label="Side", display=side.value, source=Source.ACCOUNT, note="Unchanged"),
        FieldRow(key="quantity", label="Quantity", display=f"{int(target['quantity']):,}", source=Source.ACCOUNT, note="Unchanged"),
        FieldRow(key="order_type", label="Order type", display=otype.value, source=Source.ACCOUNT, note="Unchanged"),
        FieldRow(key="trigger_price", label="Trigger price", display=_arrow(old_trig, new_trig), source=trig_source, flagged=trig_source == Source.MODEL, note=trig_note),
    ]
    if otype == OrderType.SL:
        res.fields.append(FieldRow(key="limit_price", label="Limit price", display=_arrow(old_limit, new_limit), source=limit_source, flagged=limit_source == Source.MODEL, note=limit_note))
    if protection is not None:
        res.fields.append(FieldRow(key="protection_price", label="Worst-case protected price", display=inr(protection), source=Source.RISK, note=f"Once triggered it fills at the best available price but never {'above' if side == Side.BUY else 'below'} this cap"))
    res.fields += [
        FieldRow(key="product", label="Product", display=product.value, source=Source.ACCOUNT, note="Unchanged"),
        FieldRow(key="validity", label="Validity", display=canonical.validity.value, source=Source.ACCOUNT, note="Unchanged"),
        FieldRow(key="tick_size", label="Tick size", display=inr(inst.tick_size_paise), source=Source.INSTRUMENT),
    ]
    return res


def _build_modify_cancel(s: Session, intent: OrderIntent, facts: Facts, account: AccountState, engine: str) -> BuildResult:
    if intent.action == "MODIFY" and (intent.trigger_price is not None or facts.stop_loss):
        return _build_modify_stop_loss(s, intent, facts, account)
    res = BuildResult()
    ref = (intent.target_order_ref or intent.instrument_ref or "").strip()
    target = None
    for o in account.open_orders:
        if o["broker_order_id"] == ref:
            target = o
            break
    if target is None:
        r = instruments.resolve(s, ref) if ref else None
        if r is None or r.status != "RESOLVED":
            res.clarify = (f"I could not find an open order for '{ref}'. Ask me 'what are my open orders?' to see them.", [])
            return res
        matches = [o for o in account.open_orders if o["instrument_id"] == r.instrument.instrument_id]
        if not matches:
            res.clarify = (f"You have no open order in {r.instrument.symbol}.", [])
            return res
        if len(matches) > 1:
            res.clarify = (f"You have {len(matches)} open orders in {r.instrument.symbol}. Which one?", [m["broker_order_id"] for m in matches])
            return res
        target = matches[0]
    inst = s.get(Instrument, target["instrument_id"])
    res.instrument = inst
    if intent.action == "MODIFY" and target.get("order_type") in ("SL", "SL-M"):
        trig = inr(target.get("trigger_price_paise"))
        res.clarify = (
            f"Order {target['broker_order_id']} is a {target['order_type']} stop-loss (trigger {trig}"
            + (f", limit {inr(target.get('limit_price_paise'))}" if target.get("limit_price_paise") else "")
            + f"). Do you want to move its trigger? For example 'move my stop-loss on {inst.symbol} to <new trigger>'.",
            [],
        )
        return res
    new_price = rupees_to_paise(intent.limit_price) if intent.limit_price is not None else target.get("limit_price_paise")
    canonical = CanonicalOrder(
        action=intent.action,
        account_id=account.account_id,
        instrument=instruments.handle(inst),
        side=Side(target["side"]),
        quantity=int(target["quantity"]),
        order_type=OrderType(target["order_type"]),
        product=Product(product_of(target)),
        validity=Validity(target["validity"]),
        limit_price_paise=new_price,
        trigger_price_paise=target.get("trigger_price_paise"),
        protection_price_paise=target.get("protection_price_paise"),
        target_broker_order_id=target["broker_order_id"],
        modify_from=_state_ref(target) if intent.action == "MODIFY" else None,
    )
    res.canonical = canonical
    price_src = Source.TRADER
    note = None
    if intent.action == "MODIFY":
        if intent.limit_price is None:
            res.clarify = ("What new limit price?", [])
            return res
        if not facts.mentions_number(intent.limit_price):
            price_src = Source.MODEL
            note = "This price does not appear in your request."
            res.issues.append(Issue(code="PRICE_NOT_STATED", severity="BLOCK", message="The new price was not stated by you; the AI inferred it.", field="limit_price"))
    res.fields = [
        FieldRow(key="action", label="Action", display=intent.action, source=Source.TRADER),
        FieldRow(key="target", label="Target order", display=target["broker_order_id"], source=Source.ACCOUNT, note="Resolved from your open orders"),
        FieldRow(key="instrument", label="Instrument", display=f"{inst.symbol}  ({inst.display_name})", source=Source.ACCOUNT),
        FieldRow(key="side", label="Side", display=target["side"], source=Source.ACCOUNT),
        FieldRow(key="quantity", label="Quantity", display=f"{int(target['quantity']):,}", source=Source.ACCOUNT),
    ]
    if intent.action == "MODIFY":
        res.fields.append(FieldRow(key="old_price", label="Current limit", display=inr(target.get("limit_price_paise")), source=Source.ACCOUNT))
        res.fields.append(FieldRow(key="limit_price", label="New limit price", display=inr(new_price), source=price_src, flagged=price_src == Source.MODEL, note=note))
    return res


# ------------------------------------------------------------------ verify


def _session_issue() -> Issue | None:
    st = market.session_state()
    if not st["open"]:
        return Issue(code="MARKET_CLOSED", severity="BLOCK", message=f"{st['label']}. The order will not be sent outside market hours; an after-market order would need a separate explicit request.")
    return None


def _is_021_account(s: Session, account_id: str) -> bool:
    from countersign.core.models import Account

    acct = s.get(Account, account_id)
    return acct is not None and acct.broker_kind == "021"


def verify_order(
    s: Session,
    order: CanonicalOrder,
    account: AccountState,
    *,
    reserved_paise: int = 0,
    holdings_adjust: dict[str, int] | None = None,
    skip_session: bool = False,
) -> tuple[list[Issue], OrderPreview | None]:
    issues: list[Issue] = []
    inst = s.get(Instrument, order.instrument.instrument_id)
    cfg = settings()
    if inst is None:
        return [Issue(code="UNKNOWN_INSTRUMENT", severity="BLOCK", message="Instrument is not in today's instrument master.")], None
    if inst.status != "ACTIVE":
        issues.append(Issue(code="INSTRUMENT_NOT_TRADABLE", severity="BLOCK", message=f"{inst.symbol} is {inst.status.lower()} and cannot be traded.", field="instrument"))
    if "LOOKALIKE_SYMBOL" in (inst.flags or []) or not instruments.is_ascii_symbol(inst.symbol):
        issues.append(Issue(code="LOOKALIKE_SYMBOL", severity="BLOCK", message=f"{inst.instrument_id} uses look-alike characters imitating {instruments.skeleton(inst.symbol)}.", field="instrument"))
    if inst.segment not in account.segments or not account.trading_enabled:
        issues.append(Issue(code="SEGMENT_NOT_ENABLED", severity="BLOCK", message=f"Your account is not enabled for the {inst.segment} segment."))
    if not skip_session and (si := _session_issue()):
        issues.append(si)

    tick = market.latest(inst.instrument_id)
    if tick is None:
        issues.append(Issue(code="NO_MARKET_DATA", severity="BLOCK", message=f"No live price for {inst.symbol}."))
        return issues, None
    if tick.stale:
        issues.append(Issue(code="STALE_MARKET_DATA", severity="BLOCK", message=f"Market data for {inst.symbol} is {tick.age_ms / 1000:.1f}s old (limit {cfg.tick_stale_seconds:.0f}s). Not safe to price this order."))
    if order.action != "CANCEL" and market.scenario_driven(inst.instrument_id) and _is_021_account(s, account.account_id):
        issues.append(Issue(code="SIMULATED_PRICE", severity="BLOCK", message=f"{inst.symbol} is being driven by a demo market scenario (at most two minutes). A real 021 order is never priced from simulated prices; try again when the live 021 feed is back."))
    if order.action != "CANCEL" and inst.token_021 is None and (inst.symbol in instruments.REPLAY_ONLY or "REPLAY_ONLY" in (inst.flags or [])) and _is_021_account(s, account.account_id):
        # Replay-only instruments (fictional, or delisted like TATAMOTORS) have simulated prices and no 021 token: never
        # ask a trader to countersign them on the real account (the gateway would also refuse, but only afterwards).
        hint = " Tata Motors demerged: on 021 it trades as TMPV (passenger vehicles) or TMCV (commercial vehicles)." if inst.symbol == "TATAMOTORS" else ""
        issues.append(Issue(code="INSTRUMENT_NOT_ON_021", severity="BLOCK", message=f"{inst.symbol} is not in today's 021 instrument file, so it cannot be traded on the 021 account; its price here is simulated (replay only).{hint}", field="instrument"))

    if order.action == "CANCEL":
        return issues, None

    q = order.quantity
    if q <= 0:
        issues.append(Issue(code="QUANTITY_INVALID", severity="BLOCK", message="Quantity must be a positive whole number.", field="quantity"))
    if inst.lot_size > 1 and q % inst.lot_size != 0:
        lots_down = (q // inst.lot_size) * inst.lot_size
        issues.append(Issue(code="LOT_SIZE", severity="BLOCK", message=f"{inst.symbol} trades in lots of {inst.lot_size}. {q} is not a multiple; nearest valid quantities are {lots_down} or {lots_down + inst.lot_size}.", field="quantity"))
    if inst.freeze_qty and q > inst.freeze_qty:
        n = -(-q // inst.freeze_qty)
        issues.append(Issue(code="FREEZE_QUANTITY", severity="BLOCK", message=f"{q} exceeds the exchange freeze quantity of {inst.freeze_qty}. It must be split into {n} separate orders; I will not split it silently.", field="quantity"))
    if order.order_type == OrderType.SLM and inst.segment == "FO":
        issues.append(Issue(code="SLM_NOT_ALLOWED", severity="BLOCK", message="SL-M orders are not allowed on index or stock options. Use an SL (limit) order.", field="order_type"))

    for label, price in (("limit_price", order.limit_price_paise), ("trigger_price", order.trigger_price_paise)):
        if price is None:
            continue
        if price % inst.tick_size_paise != 0:
            # Never rounded silently: the trader restates a valid price.
            down = price - price % inst.tick_size_paise
            issues.append(Issue(code="TICK_SIZE", severity="BLOCK", message=f"{inr(price)} is not a multiple of the tick size {inr(inst.tick_size_paise)}. Valid nearby prices: {inr(down)} or {inr(down + inst.tick_size_paise)}.", field=label))
        if price < inst.lower_circuit_paise or price > inst.upper_circuit_paise:
            issues.append(Issue(code="PRICE_BAND", severity="BLOCK", message=f"{inr(price)} is outside today's price band ({inr(inst.lower_circuit_paise)} to {inr(inst.upper_circuit_paise)}).", field=label))
    issues += _stop_loss_issues(order, tick.ltp_paise)

    worst = order.worst_case_price()
    if worst is None:
        worst = tick.ltp_paise
    if order.limit_price_paise is not None and order.order_type == OrderType.LIMIT:
        dev = (order.limit_price_paise - tick.ltp_paise) * 10000 // max(tick.ltp_paise, 1)
        if order.side == Side.BUY and dev > 300:
            issues.append(Issue(code="LIMIT_FAR_ABOVE_MARKET", severity="WARN", message=f"Your buy limit is {dev / 100:.2f}% above the market ({inr(tick.ltp_paise)}); it will fill immediately near market price."))
        if order.side == Side.SELL and dev < -300:
            issues.append(Issue(code="LIMIT_FAR_BELOW_MARKET", severity="WARN", message=f"Your sell limit is {-dev / 100:.2f}% below the market ({inr(tick.ltp_paise)}); it will fill immediately near market price."))

    value = q * worst
    charges_total, breakdown = charges.estimate(inst.segment, order.product.value, order.side.value, value)
    adjust = (holdings_adjust or {}).get(inst.instrument_id, 0)
    held = account.holding_qty(inst.instrument_id) + adjust
    pos = account.position_qty(inst.instrument_id)
    if account.position_rows:
        pos = sum(int(p["quantity"]) for p in account.position_rows if p["instrument_id"] == inst.instrument_id and product_of(p) != "CNC")
    pos_same_product = sum(int(p["quantity"]) for p in account.position_rows if p["instrument_id"] == inst.instrument_id and product_of(p) == order.product.value) if account.position_rows else pos
    current = held + pos
    margin = 0
    if order.side == Side.BUY:
        margin = value + charges_total if order.product == Product.CNC or inst.segment == "FO" else value // 5 + charges_total
    else:
        if order.product == Product.CNC:
            exclude = order.target_broker_order_id if order.action == "MODIFY" else None
            sellable, how = account.sellable_holding(inst.instrument_id, exclude)
            available_to_sell = sellable + adjust
            if q > available_to_sell:
                plan_note = f", {-adjust:,} already sold by earlier steps of this plan" if adjust else ""
                issues.append(Issue(code="INSUFFICIENT_HOLDINGS", severity="BLOCK", message=f"You can sell at most {max(available_to_sell, 0):,} {inst.symbol} from holdings ({how}{plan_note}). Short selling in delivery is not allowed.", field="quantity"))
            margin = charges_total
        else:
            margin = value // 5 + charges_total
            if order.action == "PLACE" and pos_same_product - q < 0:
                issues.append(Issue(code="CREATES_SHORT", severity="WARN", message=f"This intraday sell creates a short position of {q - max(pos_same_product, 0)} {inst.symbol}."))
    if order.action == "MODIFY":
        margin = max(0, margin - q * (order.limit_price_paise or 0))
    available = account.available_paise - reserved_paise
    if margin > available:
        issues.append(Issue(code="INSUFFICIENT_MARGIN", severity="BLOCK", message=f"Required {inr(margin)} but only {inr(max(available, 0))} is available.", field="quantity"))

    band = cfg.price_band_bps
    low = tick.ltp_paise * (10000 - band) // 10000
    high = tick.ltp_paise * (10000 + band) // 10000
    delta = q if order.side == Side.BUY else -q
    preview = OrderPreview(
        reference_price_paise=tick.ltp_paise,
        band_low_paise=low,
        band_high_paise=high,
        band_bps=band,
        worst_case_price_paise=worst,
        order_value_paise=value,
        estimated_margin_paise=margin,
        estimated_charges_paise=charges_total,
        charges_breakdown=breakdown,
        current_position_qty=current,
        resulting_position_qty=current + (delta if order.action == "PLACE" else 0),
        available_margin_paise=max(available, 0),
        margin_after_paise=available - margin,
        exposure_after_paise=account.exposure_paise() + (value if order.side == Side.BUY else -min(value, held * tick.ltp_paise)),
        tick_age_ms=tick.age_ms,
        session=market.session_state(),
    )
    return issues, preview


def _stop_loss_issues(order: CanonicalOrder, ltp: int) -> list[Issue]:
    """021 stop-loss rules. Buy SL: trigger above LTP and limit >= trigger. Sell SL: trigger below LTP and limit <= trigger."""
    out: list[Issue] = []
    trig = order.trigger_price_paise
    if order.order_type not in STOP_TYPES:
        if trig is not None:
            out.append(Issue(code="TRIGGER_NOT_ALLOWED", severity="BLOCK", message=f"A trigger price only applies to SL and SL-M orders; this is a {order.order_type.value} order.", field="trigger_price"))
        return out
    if trig is None:
        out.append(Issue(code="TRIGGER_REQUIRED", severity="BLOCK", message=f"A {order.order_type.value} order needs a trigger price.", field="trigger_price"))
        return out
    lim = order.limit_price_paise
    if order.order_type == OrderType.SL and lim is None:
        out.append(Issue(code="LIMIT_REQUIRED", severity="BLOCK", message="An SL order needs a limit price (or use SL-M).", field="limit_price"))
    if order.side == Side.BUY:
        if trig <= ltp:
            out.append(Issue(code="SL_TRIGGER_SIDE", severity="BLOCK", message=f"A buy stop-loss trigger must be above the last traded price {inr(ltp)}; {inr(trig)} is not, so the broker would reject it.", field="trigger_price"))
        if order.order_type == OrderType.SL and lim is not None and lim < trig:
            out.append(Issue(code="SL_LIMIT_SIDE", severity="BLOCK", message=f"For a buy stop-loss the limit price must be at or above the trigger: limit {inr(lim)} is below trigger {inr(trig)}.", field="limit_price"))
    else:
        if trig >= ltp:
            out.append(Issue(code="SL_TRIGGER_SIDE", severity="BLOCK", message=f"A sell stop-loss trigger must be below the last traded price {inr(ltp)}; {inr(trig)} is not, so the broker would reject it.", field="trigger_price"))
        if order.order_type == OrderType.SL and lim is not None and lim > trig:
            out.append(Issue(code="SL_LIMIT_SIDE", severity="BLOCK", message=f"For a sell stop-loss the limit price must be at or below the trigger: limit {inr(lim)} is above trigger {inr(trig)}.", field="limit_price"))
    return out


def _price_text(order: CanonicalOrder) -> str:
    if order.order_type == OrderType.LIMIT:
        return f"@ {inr(order.limit_price_paise)} LIMIT"
    if order.order_type == OrderType.MARKET:
        return f"at MARKET, protected at {inr(order.protection_price_paise)}"
    if order.order_type == OrderType.SLM:
        return f"SL-M trigger {inr(order.trigger_price_paise)}, protected at {inr(order.protection_price_paise)}"
    return f"SL trigger {inr(order.trigger_price_paise)} limit {inr(order.limit_price_paise)}"


def summarize(order: CanonicalOrder) -> str:
    sym = order.instrument.symbol
    if order.action == "CANCEL":
        return f"CANCEL order {order.target_broker_order_id} ({order.side.value} {order.quantity} {sym})"
    if order.action == "MODIFY":
        old = order.modify_from
        if order.order_type in STOP_TYPES and old is not None:
            parts = [f"trigger {inr(old.trigger_price_paise)} → {inr(order.trigger_price_paise)}"]
            if order.order_type == OrderType.SL:
                parts.append(f"limit {inr(old.limit_price_paise)} → {inr(order.limit_price_paise)}")
            return f"MODIFY {order.order_type.value} order {order.target_broker_order_id}: {order.side.value} {order.quantity:,} {sym}, " + ", ".join(parts)
        return f"MODIFY order {order.target_broker_order_id}: {order.side.value} {order.quantity} {sym}, new limit {inr(order.limit_price_paise)}"
    return f"{order.side.value} {order.quantity:,} {sym} {_price_text(order)} ({order.product.value}, {order.validity.value})"


# ------------------------------------------------------------------ plans


def build_plan(steps: list[CanonicalOrder], account_id: str, depends: list[int | None]) -> CanonicalPlan:
    return CanonicalPlan(
        account_id=account_id,
        steps=[PlanStepSpec(step_no=i + 1, order=o, depends_on=depends[i]) for i, o in enumerate(steps)],
    )


def verify_plan(
    s: Session, plan: CanonicalPlan, account: AccountState, *, max_steps: int = MAX_PLAN_STEPS, independent: bool = False
) -> tuple[list[list[Issue]], list[OrderPreview | None], list[Issue]]:
    plan_issues: list[Issue] = []
    if len(plan.steps) > max_steps:
        plan_issues.append(Issue(code="PLAN_TOO_LONG", severity="BLOCK", message=f"Plans are limited to {max_steps} steps; this one has {len(plan.steps)}."))
    reserved = 0
    adjust: dict[str, int] = {}
    per_issues: list[list[Issue]] = []
    previews: list[OrderPreview | None] = []
    seen_cancel: set[str] = set()
    for st in plan.steps:
        o = st.order
        if o.action == "CANCEL":
            if o.target_broker_order_id in seen_cancel:
                plan_issues.append(Issue(code="PLAN_DUPLICATE_CANCEL", severity="BLOCK", message=f"Order {o.target_broker_order_id} is cancelled twice."))
            seen_cancel.add(o.target_broker_order_id or "")
        iss, prev = verify_order(s, o, account, reserved_paise=reserved, holdings_adjust=adjust)
        if prev and o.action == "PLACE":
            if o.side == Side.BUY:
                reserved += prev.estimated_margin_paise
            else:
                if o.product == Product.CNC:
                    adjust[o.instrument.instrument_id] = adjust.get(o.instrument.instrument_id, 0) - o.quantity
                if not independent and any(x.side == Side.BUY for x in (p.order for p in plan.steps[st.step_no:])):
                    plan_issues.append(Issue(code="PLAN_USES_PROCEEDS", severity="INFO", message=f"Later buy steps run only after step {st.step_no} fills (dependency is part of the signed plan)."))
        per_issues.append(iss)
        previews.append(prev)
    for i, st in enumerate(plan.steps):
        if st.depends_on is not None and not (1 <= st.depends_on < st.step_no):
            plan_issues.append(Issue(code="PLAN_DEPENDENCY_INVALID", severity="BLOCK", message=f"Step {st.step_no} depends on an invalid step."))
    plan_issues.append(Issue(code="NO_AUTO_COMPENSATION", severity="INFO", message="If any step fails, the remaining steps are not executed and no compensating trades are invented."))
    return per_issues, previews, plan_issues


# ------------------------------------------------------------------ generated plans (exit losers, rebalance)


@dataclass
class PlanDraft:
    """Steps computed entirely by the verifier from account data and live prices. message set means: no plan."""

    orders: list[CanonicalOrder] = field(default_factory=list)
    fields: list[list[FieldRow]] = field(default_factory=list)
    issues: list[list[Issue]] = field(default_factory=list)
    notes: list[Issue] = field(default_factory=list)
    message: str | None = None
    title: str = ""


def _market_exit(account: AccountState, inst: Instrument, side: Side, qty: int, product: Product, ltp: int) -> CanonicalOrder:
    return CanonicalOrder(
        action="PLACE",
        account_id=account.account_id,
        instrument=instruments.handle(inst),
        side=side,
        quantity=qty,
        order_type=OrderType.MARKET,
        product=product,
        validity=Validity.DAY,
        protection_price_paise=protection_price(side, ltp, inst.tick_size_paise),
    )


def _type_rows(order: CanonicalOrder, facts: Facts, ltp: int) -> list[FieldRow]:
    stated = facts.market
    return [
        FieldRow(
            key="order_type",
            label="Order type",
            display="MARKET",
            source=Source.TRADER if stated else Source.DEFAULT,
            note=None if stated else "You gave no price, so each step is a price-protected market order; its worst case is shown below.",
        ),
        FieldRow(
            key="protection_price",
            label="Worst-case protected price",
            display=inr(order.protection_price_paise),
            source=Source.RISK,
            note=f"Fills at the best available price but never {'above' if order.side == Side.BUY else 'below'} this cap ({settings().market_protection_bps / 100:.2f}% from the live price {inr(ltp)}). Worst case for the step: {inr((order.protection_price_paise or 0) * order.quantity)}.",
        ),
    ]


def _open_order_warnings(account: AccountState, inst: Instrument) -> list[Issue]:
    left = [o for o in account.open_orders if o["instrument_id"] == inst.instrument_id]
    if not left:
        return []
    ids = ", ".join(f"{o['broker_order_id']} ({o['side']} {int(o['quantity']):,} {o['order_type']})" for o in left)
    return [Issue(code="OPEN_ORDERS_REMAIN", severity="WARN", message=f"You also have open {inst.symbol} orders ({ids}). This plan does not cancel them; if one fills later it opens a new position. Cancel them separately if you no longer want them.")]


def build_exit_plan(s: Session, account: AccountState, facts: Facts, *, intraday_only: bool) -> PlanDraft:
    """'Exit all my losing intraday positions': one exit per position whose unrealised P&L at the live price is
    below zero, for exactly its square-off quantity. Steps are independent and ordered by loss, largest first."""
    products = {"MIS"} if intraday_only else {"MIS", "NRML"}
    label = "intraday (MIS)" if intraday_only else "intraday (MIS) and NRML"
    if facts.price is not None or facts.quantity is not None:
        return PlanDraft(message="One price or quantity cannot apply to several positions. Say 'exit all my losing intraday positions' to close each one in full with a price-protected market order, or exit positions one by one with your own prices.")
    rows = [p for p in account.position_rows if product_of(p) in products and int(p["quantity"]) != 0]
    if not rows:
        return PlanDraft(message=f"You have no open {label} positions, so there is nothing to exit. No plan was drafted.")
    evaluated = []
    unpriced = []
    for p in rows:
        inst = s.get(Instrument, p["instrument_id"])
        tick = market.latest(p["instrument_id"])
        sym = inst.symbol if inst else str(p.get("symbol") or p["instrument_id"])
        if inst is None or tick is None or tick.stale:
            unpriced.append(sym)
            continue
        q = int(p["quantity"])
        avg = int(p["avg_price_paise"])
        evaluated.append((p, inst, tick, q, avg, (tick.ltp_paise - avg) * q))
    losers = sorted((e for e in evaluated if e[5] < 0), key=lambda e: e[5])
    if not losers:
        checked = ", ".join(f"{e[1].symbol} {inr(e[5], True)}" for e in evaluated)
        msg = f"None of your {label} positions is losing at current prices" + (f" ({checked})" if checked else "") + ". No plan was drafted."
        if unpriced:
            msg += f" I could not price {', '.join(unpriced)} (no live tick), so they were not checked."
        return PlanDraft(message=msg)
    d = PlanDraft(title=f"Exit {len(losers)} losing {label} position(s)")
    for p, inst, tick, q, avg, pnl in losers:
        sq = p.get("square_off_quantity")
        sq = int(sq) if sq not in (None, 0) else -q  # 0 or absent: the broker did not report it
        side = Side.SELL if sq < 0 else Side.BUY
        qty = abs(sq)
        order = _market_exit(account, inst, side, qty, Product(product_of(p)), tick.ltp_paise)
        step_issues = _open_order_warnings(account, inst)
        if sq != -q:
            step_issues.append(Issue(code="SQUARE_OFF_MISMATCH", severity="BLOCK", message=f"The broker's square-off quantity ({sq:+,}) does not close your net position ({q:+,}); refusing to guess which is right.", field="quantity"))
        rows_ = [
            FieldRow(key="side", label="Side", display=side.value, source=Source.ACCOUNT, note=f"Closes your {'long' if q > 0 else 'short'} position"),
            FieldRow(key="instrument", label="Instrument", display=f"{inst.symbol}  ({inst.display_name})", source=Source.ACCOUNT, note="From your open positions"),
            FieldRow(key="instrument_id", label="Instrument ID", display=inst.instrument_id, source=Source.INSTRUMENT),
            FieldRow(key="quantity", label="Quantity", display=f"{qty:,}", source=Source.ACCOUNT, note=f"Computed from your positions: the exact square-off quantity of your net position {q:+,}"),
            FieldRow(key="unrealised_pnl", label="Unrealised P&L now", display=inr(pnl, True), source=Source.FEED, note=f"(live {inr(tick.ltp_paise)} - average {inr(avg)}) x {q:+,}"),
            *_type_rows(order, facts, tick.ltp_paise),
            FieldRow(key="product", label="Product", display=order.product.value, source=Source.ACCOUNT, note="Same product as the position"),
            FieldRow(key="validity", label="Validity", display="DAY", source=Source.DEFAULT),
            FieldRow(key="lot_size", label="Lot size", display=str(inst.lot_size), source=Source.INSTRUMENT),
        ]
        d.orders.append(order)
        d.fields.append(rows_)
        d.issues.append(step_issues)
    scope = "Holdings are not touched." + (" NRML positions are not touched." if intraday_only else "")
    d.notes.append(
        Issue(
            code="EXIT_SCOPE",
            severity="INFO",
            message=f"Exits every {label} position whose unrealised P&L is below zero at the live price: {len(losers)} of {len(evaluated)} checked. Each step closes one position in full. {scope} Steps are independent (none waits for another) and ordered by loss, largest first.",
        )
    )
    if unpriced:
        d.notes.append(Issue(code="EXIT_UNPRICED", severity="WARN", message=f"No live price for {', '.join(unpriced)}, so I could not tell whether they are losing; they are not in this plan."))
    return d


def build_rebalance_plan(s: Session, account: AccountState, facts: Facts, *, max_pct: Decimal) -> PlanDraft:
    """'Rebalance so no single stock is more than 20% of my portfolio': deterministic sells only (see
    portfolio.rebalance_sells for the formula), from holdings valued at live prices."""
    if not (Decimal(0) < max_pct < Decimal(100)):
        return PlanDraft(message="The maximum weight must be between 0% and 100%.")
    max_bps = int(max_pct * 100)
    pct_txt = f"{max_pct.normalize():f}%"
    held = [(iid, h) for iid, h in account.holdings.items() if int(h.get("quantity") or 0) > 0]
    if not held:
        return PlanDraft(message="You have no holdings to rebalance. No plan was drafted.")
    rows = []
    missing = []
    meta: dict[str, tuple[Instrument, int]] = {}
    for iid, h in held:
        inst = s.get(Instrument, iid)
        tick = market.latest(iid)
        if inst is None or tick is None or tick.stale:
            missing.append(inst.symbol if inst else str(h.get("symbol") or iid))
            continue
        meta[iid] = (inst, tick.ltp_paise)
        rows.append({"key": iid, "qty": int(h["quantity"]), "price": tick.ltp_paise, "lot": max(int(inst.lot_size or 1), 1)})
    if missing:
        return PlanDraft(message=f"I need a live price for every holding to compute weights, and there is none for {', '.join(missing)}. No plan was drafted.")
    r = portfolio.rebalance_sells(rows, max_bps)

    def w(bps_frac) -> str:
        return f"{float(bps_frac) / 100:.2f}%"

    if r["status"] == "WITHIN":
        top = max(r["before"].items(), key=lambda kv: kv[1])
        return PlanDraft(message=f"Already within the limit: no holding is above {pct_txt} of your holdings value ({inr(r['value_before'])} at live prices). The largest is {meta[top[0]][0].symbol} at {w(top[1])}. No plan was drafted.")
    if r["status"] == "INFEASIBLE":
        return PlanDraft(message=f"{r['reason']} No plan was drafted.")
    d = PlanDraft(title=f"Rebalance to at most {pct_txt} per stock")
    order_keys = sorted(r["sells"], key=lambda k: r["before"][k], reverse=True)
    for k in order_keys:
        sell = r["sells"][k]
        inst, ltp = meta[k]
        q = account.holding_qty(k)
        order = _market_exit(account, inst, Side.SELL, sell, Product.CNC, ltp)
        rows_ = [
            FieldRow(key="side", label="Side", display="SELL", source=Source.ACCOUNT, note="Sells only: this plan never buys"),
            FieldRow(key="instrument", label="Instrument", display=f"{inst.symbol}  ({inst.display_name})", source=Source.ACCOUNT, note="From your holdings"),
            FieldRow(key="instrument_id", label="Instrument ID", display=inst.instrument_id, source=Source.INSTRUMENT),
            FieldRow(key="quantity", label="Quantity", display=f"{sell:,}", source=Source.ACCOUNT, note=f"Computed from your holdings: sell {sell:,} of {q:,} so {inst.symbol} is at most {pct_txt} of what remains (rounded up to {'whole shares' if inst.lot_size <= 1 else f'whole lots of {inst.lot_size}'})"),
            FieldRow(key="weight", label="Weight", display=f"{w(r['before'][k])} → {w(r['after'][k])}", source=Source.FEED, note=f"Value at live {inr(ltp)}: {inr(q * ltp)} → {inr((q - sell) * ltp)}"),
            *_type_rows(order, facts, ltp),
            FieldRow(key="product", label="Product", display="CNC", source=Source.ACCOUNT, note="Delivery holding"),
            FieldRow(key="validity", label="Validity", display="DAY", source=Source.DEFAULT),
        ]
        d.orders.append(order)
        d.fields.append(rows_)
        d.issues.append(_open_order_warnings(account, inst))
    after = ", ".join(f"{meta[k][0].symbol} {w(r['before'][k])} → {w(r['after'][k])}" for k in sorted(r["after"], key=lambda k: r["after"][k], reverse=True))
    d.notes.append(
        Issue(
            code="REBALANCE_METHOD",
            severity="INFO",
            message=(
                f"Method: portfolio value = sum of quantity x live price over your holdings only (cash and positions are not counted). Sells only, no buys. "
                f"Each stock above {pct_txt} is sold down by the fewest whole shares (rounded up to its lot size) so that it is at most {pct_txt} of the holdings value that remains after all the sells. "
                f"Holdings value {inr(r['value_before'])} → {inr(r['value_after'])}. Fills at market can differ slightly from the live prices used here."
            ),
        )
    )
    d.notes.append(Issue(code="REBALANCE_RESULT", severity="INFO", message=f"Weights now → after this plan, at live prices: {after}."))
    return d


# ------------------------------------------------------------------ mandates


def build_mandate(
    s: Session, intent: MandateIntent, facts: Facts, account: AccountState, *, engine: str
) -> tuple[CanonicalMandate | None, BuildResult, list[Issue]]:
    issues: list[Issue] = []
    if intent.scope == "ALL_HOLDINGS":
        return _build_holdings_alert(intent, facts, account)
    r = instruments.resolve(s, intent.instrument_ref or "")
    br = BuildResult()
    if r.status != "RESOLVED":
        br.clarify = (r.message + " Please give the exact symbol.", [c.symbol for c in r.candidates][:6])
        return None, br, issues
    inst = r.instrument
    ask = _already_true_question(intent, inst)
    if ask is not None:
        # Never arm an instruction that would fire on the next tick: the trader chooses an order now or a new trigger.
        br.clarify = ask
        br.instrument = inst
        return None, br, issues
    expires = (datetime.now(UTC) + timedelta(days=max(1, min(intent.expires_days, 30)))).replace(microsecond=0)
    template = None
    if intent.kind == "ORDER":
        if intent.order is None:
            br.clarify = ("What order should fire when the condition is met?", [])
            return None, br, issues
        br = build_order(s, intent.order, facts, account, engine=engine)
        if br.canonical is None:
            return None, br, issues
        template = br.canonical
        threshold = rupees_to_paise(intent.threshold_price)
        if template.order_type != OrderType.LIMIT:
            issues.append(Issue(code="MANDATE_NEEDS_LIMIT", severity="BLOCK", message="Standing instructions must use a limit price so the worst case is bounded."))
        predicate = MandatePredicate(field="LTP", op=intent.op, threshold=threshold)
        if not facts.mentions_number(intent.threshold_price):
            issues.append(Issue(code="THRESHOLD_NOT_STATED", severity="BLOCK", message="The trigger price was not stated by you; the AI inferred it.", field="threshold"))
        mandate = CanonicalMandate(
            account_id=account.account_id,
            kind="ORDER",
            instrument=instruments.handle(inst),
            predicate=predicate,
            template=template,
            max_quantity=template.quantity,
            price_limit_paise=template.limit_price_paise,
            one_shot=True,
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )
    else:
        if intent.field == "DROP_FROM_AVG_BPS":
            pct = intent.drop_pct or Decimal("5")
            if not facts.mentions_number(pct):
                issues.append(Issue(code="THRESHOLD_NOT_STATED", severity="BLOCK", message="The percentage was not stated by you.", field="threshold"))
            if account.holding_qty(inst.instrument_id) == 0:
                issues.append(Issue(code="NOT_A_HOLDING", severity="BLOCK", message=f"You do not hold {inst.symbol}; a drop-from-average alert needs a holding."))
            predicate = MandatePredicate(field="DROP_FROM_AVG_BPS", op="GTE", threshold=int(pct * 100))
        else:
            predicate = MandatePredicate(field="LTP", op=intent.op, threshold=rupees_to_paise(intent.threshold_price))
        mandate = CanonicalMandate(
            account_id=account.account_id,
            kind="ALERT",
            instrument=instruments.handle(inst),
            predicate=predicate,
            one_shot=True,
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )
        br.instrument = inst
    br.instrument = inst
    tick = market.latest(inst.instrument_id)
    if tick and mandate.predicate.field == "LTP":
        already = (tick.ltp_paise <= mandate.predicate.threshold) if mandate.predicate.op == "LTE" else (tick.ltp_paise >= mandate.predicate.threshold)
        if already:
            issues.append(Issue(code="CONDITION_ALREADY_TRUE", severity="WARN", message=f"The condition is already true at the current price {inr(tick.ltp_paise)}; it would trigger on the next tick."))
    return mandate, br, issues


def _rupees_text(paise: int) -> str:
    """₹1,400 for whole rupees, ₹1,213.40 otherwise (Indian grouping), in a form the parser reads back."""
    txt = inr(paise)
    return txt[:-3] if txt.endswith(".00") else txt


_PRODUCT_WORD = {Product.CNC: " delivery", Product.MIS: " intraday"}


def _already_true_question(intent: MandateIntent, inst) -> tuple[str, list[str]] | None:
    """A clarifying question when a price condition is already true at the live price, or None. The options are
    complete sentences: the order placed now (a normal countersigned order) or a trigger on the far side of the price."""
    if intent.field != "LTP" or intent.threshold_price is None:
        return None
    if intent.kind == "ORDER" and (intent.order is None or intent.order.side is None or not intent.order.quantity):
        return None
    tick = market.latest(inst.instrument_id)
    if tick is None:
        return None
    threshold = rupees_to_paise(intent.threshold_price)
    ltp = tick.ltp_paise
    below = intent.op == "LTE"
    if not ((ltp <= threshold) if below else (ltp >= threshold)):
        return None
    sym = inst.symbol
    cond = f"{'falls below' if below else 'rises above'} {_rupees_text(threshold)}"
    # A new trigger about 2% beyond the live price, in whole rupees, on the side the condition needs.
    new_trig = (ltp * 98 // 100) // 100 * 100 if below else -(-(ltp * 102 // 100) // 100) * 100
    move = "drops below" if below else "rises above"
    if intent.kind == "ALERT":
        options = [f"Alert me if {sym} {'falls below' if below else 'rises above'} {_rupees_text(new_trig)}"]
        q = (
            f"{sym} is at {inr(ltp)} right now, so the condition '{cond}' is already true and the alert would fire on the next price update. "
            f"I have not set it. Choose a level {'below' if below else 'above'} the current price, or tell me a different one."
        )
        return q, options
    o = intent.order
    side = o.side.value.lower()
    qty = int(o.quantity)
    product = _PRODUCT_WORD.get(o.product, "") if o.product else ""
    limit = rupees_to_paise(o.limit_price) if o.limit_price is not None else threshold
    lo, hi = inst.lower_circuit_paise or 0, inst.upper_circuit_paise or 0
    in_band = (not hi or lo <= limit <= hi) and (inst.tick_size_paise <= 0 or limit % inst.tick_size_paise == 0)
    # The live price can sit off the tick grid: round it so a buy stays marketable (up) and a sell too (down).
    now_limit = limit if in_band else round_to_tick(ltp, inst.tick_size_paise, "up" if o.side == Side.BUY else "down")
    options = [
        f"{side.capitalize()} {qty} {sym} now at limit {_rupees_text(now_limit)}{product}",
        f"If {sym} {move} {_rupees_text(new_trig)}, {side} {qty}{product}",
    ]
    q = (
        f"{sym} is at {inr(ltp)} right now, so the condition '{cond}' is already true: armed as written, this instruction would "
        f"{side} {qty} {sym} on the next price update. I have not armed it. Do you want to {side} now with a limit order you countersign, "
        f"or set a trigger {'below' if below else 'above'} the current price?"
    )
    if not in_band:
        q += f" Your limit {_rupees_text(limit)} is outside today's price band or tick size, so the order now uses the live price as its limit."
    return q, options


ALL_HOLDINGS_HANDLE = InstrumentHandle(
    instrument_id=ALL_HOLDINGS_ID,
    segment="EQ",
    symbol="ALL HOLDINGS",
    display_name="Every stock you hold when each price update arrives",
)


def _build_holdings_alert(intent: MandateIntent, facts: Facts, account: AccountState) -> tuple[CanonicalMandate | None, BuildResult, list[Issue]]:
    """'Tell me when any holding falls 3% in a day': an ALERT over every holding (never an order), firing at most once
    per holding per IST trading day. The trader must have asked for it; the percentage must be one they stated."""
    br = BuildResult()
    issues: list[Issue] = []
    if intent.kind != "ALERT":
        br.clarify = ("Standing orders across all holdings are not supported. Name one stock for an order instruction, or ask for an alert on your holdings.", [])
        return None, br, issues
    if detect_holdings_alert(facts.text) is None:
        issues.append(Issue(code="SCOPE_NOT_STATED", severity="BLOCK", message="Your message does not ask for an alert on all of your holdings; the AI chose that scope.", field="instrument"))
    pct = intent.drop_pct
    if pct is None:
        br.clarify = ("By how much should a holding fall before I alert you? For example 'tell me when any holding falls 3% in a day'.", [])
        return None, br, issues
    pct = abs(Decimal(str(pct)))
    if not (Decimal(0) < pct < Decimal(100)):
        br.clarify = ("The fall must be between 0% and 100%.", [])
        return None, br, issues
    if not facts.mentions_number(pct):
        issues.append(Issue(code="THRESHOLD_NOT_STATED", severity="BLOCK", message=f"The percentage {pct.normalize():f}% was not stated by you; the AI inferred it.", field="threshold"))
    if intent.field == "DROP_FROM_AVG_BPS":
        predicate = MandatePredicate(field="DROP_FROM_AVG_BPS", op="GTE", threshold=int(pct * 100))
    else:
        predicate = MandatePredicate(field="DAY_CHANGE_BPS", op="LTE", threshold=-int(pct * 100))
    expires = (datetime.now(UTC) + timedelta(days=max(1, min(intent.expires_days, 30)))).replace(microsecond=0)
    mandate = CanonicalMandate(
        account_id=account.account_id,
        kind="ALERT",
        instrument=ALL_HOLDINGS_HANDLE,
        predicate=predicate,
        one_shot=False,
        expires_at=expires.isoformat().replace("+00:00", "Z"),
        scope="ALL_HOLDINGS",
        fire_policy="ONCE_PER_HOLDING_PER_DAY",
    )
    n = sum(1 for h in account.holdings.values() if int(h.get("quantity") or 0) > 0)
    if n == 0:
        issues.append(Issue(code="NO_HOLDINGS_YET", severity="WARN", message="You hold no stocks right now; the alert watches whatever you hold when each price update arrives."))
    issues.append(Issue(code="ALERT_SCOPE", severity="INFO", message=f"Watches every stock you hold when each price update arrives ({n} now). Fires at most once per holding per trading day (IST), survives restarts, and never places an order."))
    return mandate, br, issues


def describe_predicate(m: CanonicalMandate) -> str:
    p = m.predicate
    who = "any holding" if m.scope == "ALL_HOLDINGS" else m.instrument.symbol
    if p.field == "LTP":
        return f"{m.instrument.symbol} LTP {'<=' if p.op == 'LTE' else '>='} {inr(p.threshold)}"
    if p.field == "DAY_CHANGE_BPS":
        return f"{who} is down {abs(p.threshold) / 100:.2f}% or more today (vs previous close)"
    return f"{who} falls {p.threshold / 100:.2f}% or more below your average price"


def mandate_summary(m: CanonicalMandate) -> str:
    cond = describe_predicate(m)
    if m.kind == "ALERT" and m.fire_policy == "ONCE_PER_HOLDING_PER_DAY":
        return f"ALERT when {cond}: at most once per holding per trading day, never places an order (expires {m.expires_at[:10]})"
    if m.kind == "ALERT":
        return f"ALERT once when {cond} (expires {m.expires_at[:10]})"
    t = m.template
    return f"When {cond}: {t.side.value} {t.quantity:,} {t.instrument.symbol} @ {inr(t.limit_price_paise)} LIMIT, once (max {m.max_quantity}, expires {m.expires_at[:10]})"


def list_instruments(s: Session, q: str | None) -> list[Instrument]:
    stmt = select(Instrument).where(Instrument.segment.in_(["EQ", "IDX"]))
    rows = s.execute(stmt).scalars().all()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r.symbol.lower() or ql in r.display_name.lower() or any(ql in a for a in (r.aliases or []))]
    return rows
