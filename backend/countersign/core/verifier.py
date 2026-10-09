"""Deterministic builder and verifier. Independent of any model.

build_*: turns an untrusted intent into a canonical object, attaching field provenance and running the misread
guard against the facts the trader actually stated.
verify_*: validates the canonical object (lot, tick, freeze, price band, margin, holdings, instrument status,
session, data freshness, plan consistency, mandate scope) and computes the wallet-style preview.
A BLOCK issue means no approval can be created and the broker is never called."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from countersign.core import charges, instruments, market
from countersign.core.config import settings
from countersign.core.models import Instrument
from countersign.core.money import inr, rupees_to_paise
from countersign.core.parser import Facts
from countersign.core.schemas import (
    CanonicalMandate,
    CanonicalOrder,
    CanonicalPlan,
    Disagreement,
    FieldRow,
    Issue,
    MandateIntent,
    MandatePredicate,
    OrderIntent,
    OrderPreview,
    OrderType,
    PlanStepSpec,
    Product,
    Side,
    Source,
    Validity,
)


# ------------------------------------------------------------------ account state


@dataclass
class AccountState:
    account_id: str
    cash_paise: int = 0
    available_paise: int = 0
    used_margin_paise: int = 0
    holdings: dict[str, dict] = field(default_factory=dict)
    positions: dict[str, dict] = field(default_factory=dict)
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
        st.open_orders = gateway.orders(status="open")
        return st

    def holding_qty(self, instrument_id: str) -> int:
        h = self.holdings.get(instrument_id)
        return int(h["quantity"]) if h else 0

    def position_qty(self, instrument_id: str) -> int:
        p = self.positions.get(instrument_id)
        return int(p["quantity"]) if p else 0

    def pending_sell_qty(self, instrument_id: str) -> int:
        return sum(
            int(o["pending_quantity"])
            for o in self.open_orders
            if o["instrument_id"] == instrument_id and o["side"] == "SELL" and o["product"] == "CNC"
        )

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
    if intent.target_order_ref and intent.target_order_ref.startswith("LOTS:"):
        lots = int(intent.target_order_ref.split(":")[1])
        qty = lots * inst.lot_size
        qty_note = f"{lots} lot(s) x lot size {inst.lot_size} (instrument metadata)"
    elif replace_source is not None and intent.quantity is None:
        qty = int(replace_source["quantity"])
        qty_source, qty_note = Source.ACCOUNT, "Copied from the cancelled order."
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

    trigger_paise = rupees_to_paise(intent.trigger_price) if intent.trigger_price is not None else None

    tick = market.latest(inst.instrument_id)
    protection = None
    if otype in (OrderType.MARKET, OrderType.SLM):
        if tick is None:
            issues.append(Issue(code="NO_MARKET_DATA", severity="BLOCK", message=f"No live price for {inst.symbol}; a market order cannot be price-protected.", field="order_type"))
        else:
            bps = settings().market_protection_bps
            raw = tick.ltp_paise * (10000 + bps) // 10000 if side == Side.BUY else tick.ltp_paise * (10000 - bps) // 10000
            from countersign.core.money import round_to_tick

            protection = round_to_tick(raw, inst.tick_size_paise, "up" if side == Side.BUY else "down")

    product = intent.product
    product_source = Source.TRADER
    if product is None:
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
        FieldRow(key="order_type", label="Order type", display=otype.value, source=type_source, flagged=type_source == Source.MODEL),
    ]
    if limit_paise is not None:
        res.fields.append(FieldRow(key="limit_price", label="Limit price", display=inr(limit_paise), source=price_source, flagged=price_source == Source.MODEL, note=price_note))
    if trigger_paise is not None:
        res.fields.append(FieldRow(key="trigger_price", label="Trigger price", display=inr(trigger_paise), source=Source.TRADER if facts.mentions_number(intent.trigger_price) else Source.MODEL))
    if protection is not None:
        res.fields.append(FieldRow(key="protection_price", label="Worst-case protected price", display=inr(protection), source=Source.RISK, note=f"Market order capped at {settings().market_protection_bps / 100:.2f}% from the live price"))
    res.fields += [
        FieldRow(key="product", label="Product", display=product.value, source=product_source),
        FieldRow(key="validity", label="Validity", display=validity.value, source=Source.DEFAULT if intent.validity is None else Source.TRADER),
        FieldRow(key="lot_size", label="Lot size", display=str(inst.lot_size), source=Source.INSTRUMENT),
        FieldRow(key="tick_size", label="Tick size", display=inr(inst.tick_size_paise), source=Source.INSTRUMENT),
    ]
    return res


def _build_modify_cancel(s: Session, intent: OrderIntent, facts: Facts, account: AccountState, engine: str) -> BuildResult:
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
    new_price = rupees_to_paise(intent.limit_price) if intent.limit_price is not None else target.get("limit_price_paise")
    canonical = CanonicalOrder(
        action=intent.action,
        account_id=account.account_id,
        instrument=instruments.handle(inst),
        side=Side(target["side"]),
        quantity=int(target["quantity"]),
        order_type=OrderType(target["order_type"]),
        product=Product(target["product"]),
        validity=Validity(target["validity"]),
        limit_price_paise=new_price,
        trigger_price_paise=target.get("trigger_price_paise"),
        protection_price_paise=target.get("protection_price_paise"),
        target_broker_order_id=target["broker_order_id"],
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
            down = price - price % inst.tick_size_paise
            issues.append(Issue(code="TICK_SIZE", severity="BLOCK", message=f"{inr(price)} is not a multiple of the tick size {inr(inst.tick_size_paise)}. Valid nearby prices: {inr(down)} or {inr(down + inst.tick_size_paise)}.", field=label))
        if price < inst.lower_circuit_paise or price > inst.upper_circuit_paise:
            issues.append(Issue(code="PRICE_BAND", severity="BLOCK", message=f"{inr(price)} is outside today's price band ({inr(inst.lower_circuit_paise)} to {inr(inst.upper_circuit_paise)}).", field=label))

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
    held = account.holding_qty(inst.instrument_id) + (holdings_adjust or {}).get(inst.instrument_id, 0)
    pos = account.position_qty(inst.instrument_id)
    current = held + pos
    margin = 0
    if order.side == Side.BUY:
        margin = value + charges_total if order.product == Product.CNC or inst.segment == "FO" else value // 5 + charges_total
    else:
        if order.product == Product.CNC:
            available_to_sell = held - account.pending_sell_qty(inst.instrument_id)
            if q > available_to_sell:
                issues.append(Issue(code="INSUFFICIENT_HOLDINGS", severity="BLOCK", message=f"You can sell at most {max(available_to_sell, 0)} {inst.symbol} from holdings (held {held}, already pending sell {account.pending_sell_qty(inst.instrument_id)}). Short selling in delivery is not allowed.", field="quantity"))
            margin = charges_total
        else:
            margin = value // 5 + charges_total
            if pos - q < 0:
                issues.append(Issue(code="CREATES_SHORT", severity="WARN", message=f"This intraday sell creates a short position of {q - max(pos, 0)} {inst.symbol}."))
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


def summarize(order: CanonicalOrder) -> str:
    sym = order.instrument.symbol
    if order.action == "CANCEL":
        return f"CANCEL order {order.target_broker_order_id} ({order.side.value} {order.quantity} {sym})"
    if order.action == "MODIFY":
        return f"MODIFY order {order.target_broker_order_id}: {order.side.value} {order.quantity} {sym}, new limit {inr(order.limit_price_paise)}"
    price = (
        f"@ {inr(order.limit_price_paise)} LIMIT"
        if order.order_type == OrderType.LIMIT
        else f"at MARKET, protected at {inr(order.protection_price_paise)}"
        if order.order_type == OrderType.MARKET
        else f"{order.order_type.value} trigger {inr(order.trigger_price_paise)} limit {inr(order.limit_price_paise)}"
    )
    return f"{order.side.value} {order.quantity:,} {sym} {price} ({order.product.value}, {order.validity.value})"


# ------------------------------------------------------------------ plans


def build_plan(steps: list[CanonicalOrder], account_id: str, depends: list[int | None]) -> CanonicalPlan:
    return CanonicalPlan(
        account_id=account_id,
        steps=[PlanStepSpec(step_no=i + 1, order=o, depends_on=depends[i]) for i, o in enumerate(steps)],
    )


def verify_plan(s: Session, plan: CanonicalPlan, account: AccountState) -> tuple[list[list[Issue]], list[OrderPreview | None], list[Issue]]:
    plan_issues: list[Issue] = []
    if len(plan.steps) > 6:
        plan_issues.append(Issue(code="PLAN_TOO_LONG", severity="BLOCK", message="Plans are limited to 6 steps."))
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
                adjust[o.instrument.instrument_id] = adjust.get(o.instrument.instrument_id, 0) - o.quantity
                if any(x.side == Side.BUY for x in (p.order for p in plan.steps[st.step_no:])):
                    plan_issues.append(Issue(code="PLAN_USES_PROCEEDS", severity="INFO", message=f"Later buy steps run only after step {st.step_no} fills (dependency is part of the signed plan)."))
        per_issues.append(iss)
        previews.append(prev)
    for i, st in enumerate(plan.steps):
        if st.depends_on is not None and not (1 <= st.depends_on < st.step_no):
            plan_issues.append(Issue(code="PLAN_DEPENDENCY_INVALID", severity="BLOCK", message=f"Step {st.step_no} depends on an invalid step."))
    plan_issues.append(Issue(code="NO_AUTO_COMPENSATION", severity="INFO", message="If any step fails, the remaining steps are not executed and no compensating trades are invented."))
    return per_issues, previews, plan_issues


# ------------------------------------------------------------------ mandates


def build_mandate(
    s: Session, intent: MandateIntent, facts: Facts, account: AccountState, *, engine: str
) -> tuple[CanonicalMandate | None, BuildResult, list[Issue]]:
    issues: list[Issue] = []
    r = instruments.resolve(s, intent.instrument_ref)
    br = BuildResult()
    if r.status != "RESOLVED":
        br.clarify = (r.message + " Please give the exact symbol.", [c.symbol for c in r.candidates][:6])
        return None, br, issues
    inst = r.instrument
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


def describe_predicate(m: CanonicalMandate) -> str:
    p = m.predicate
    if p.field == "LTP":
        return f"{m.instrument.symbol} LTP {'<=' if p.op == 'LTE' else '>='} {inr(p.threshold)}"
    return f"{m.instrument.symbol} falls {p.threshold / 100:.2f}% or more below your average price"


def mandate_summary(m: CanonicalMandate) -> str:
    cond = describe_predicate(m)
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
