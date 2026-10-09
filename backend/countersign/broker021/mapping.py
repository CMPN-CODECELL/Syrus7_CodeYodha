"""Pure translation between Countersign's broker interface (the paper broker's /v1 JSON) and the 021 REST API.

Requests use short exchange names (NSE, NSEFO, BSE, BSEFO); responses and the instrument file use internal names
(NSECM, NSEFO, BSEEQ, BSEEQD). The side is the sign of qty. Prices are paise on both sides."""

from __future__ import annotations

from datetime import UTC, datetime

PRODUCT_TO_021 = {"CNC": "CNC", "MIS": "INTRADAY", "NRML": "NRML"}
PRODUCT_FROM_021 = {v: k for k, v in PRODUCT_TO_021.items()}
VALIDITY_TO_021 = {"DAY": "Day", "IOC": "IOC"}
VALIDITY_FROM_021 = {v: k for k, v in VALIDITY_TO_021.items()}
REQUEST_EXCHANGE = {"NSECM": "NSE", "NSEFO": "NSEFO", "BSEEQ": "BSE", "BSEEQD": "BSEFO"}
RESPONSE_EXCHANGE = {v: k for k, v in REQUEST_EXCHANGE.items()}
SEGMENT_OF = {"NSECM": "EQ", "BSEEQ": "EQ", "NSEFO": "FO", "BSEEQD": "FO", "NSEIDX": "IDX", "BSEIDX": "IDX"}

# 021 order status -> Countersign status. Non-terminal states stay OPEN (or TRIGGER_PENDING / PARTIALLY_FILLED,
# decided from the fill and book in order_status); the 021 status is always kept alongside as status_021.
TERMINAL_021 = {"Executed": "COMPLETE", "Rejected": "REJECTED", "Cancelled": "CANCELLED"}
OPEN_021 = {"Received", "Placed", "Pending", "Frozen", "SentForModification", "SentForCancellation", "AcceptedForAMO"}

# HTTP outcomes of a write call, as classified by the gateway.
DEFINITE_REJECT_HTTP = {400, 403, 404, 421, 422}
AMBIGUOUS_HTTP = {429, 500, 502, 503, 504}
RISK_WORDS = ("fund", "margin", "circuit", "lot", "tick", "price band", "price range", "trigger", "insufficient", "rms", "risk", "freeze", "multiple of")


class MappingError(ValueError):
    """The order cannot be expressed on 021 exactly as signed; it is refused rather than approximated."""


def is_risk_rejection(text: str | None) -> bool:
    low = (text or "").lower()
    return any(w in low for w in RISK_WORDS)


def to_021_order(body: dict, exchange_021: str, token: int) -> dict:
    """The exact 021 order for a signed Countersign order. Never changes the order's meaning:
    LIMIT  -> RL at the limit price
    MARKET -> RL at the signed protection price (a marketable limit), or price 0 only when no protection was signed
    SL     -> SL with the signed limit price and trigger
    SL-M   -> SL at the signed protection price (or 0 when none) with the signed trigger"""
    req_exch = REQUEST_EXCHANGE.get(exchange_021)
    if req_exch is None:
        raise MappingError(f"{exchange_021} is not a tradable 021 exchange (indices are data only).")
    qty = int(body["quantity"])
    if qty <= 0:
        raise MappingError("Quantity must be positive.")
    side = body["side"]
    if side not in ("BUY", "SELL"):
        raise MappingError(f"Unknown side {side!r}.")
    product = PRODUCT_TO_021.get(body["product"])
    if product is None:
        raise MappingError(f"Product {body['product']!r} has no 021 equivalent.")
    validity = VALIDITY_TO_021.get(str(body.get("validity") or "DAY").upper())
    if validity is None:
        raise MappingError(f"Validity {body.get('validity')!r} has no 021 equivalent.")
    otype = body["order_type"]
    limit = body.get("limit_price_paise")
    trigger = body.get("trigger_price_paise")
    protection = body.get("protection_price_paise")
    if otype == "LIMIT":
        if not limit:
            raise MappingError("A limit order needs a limit price.")
        book, price, trig = "RL", int(limit), 0
    elif otype == "MARKET":
        book, price, trig = "RL", int(protection or 0), 0
    elif otype == "SL":
        if not limit or not trigger:
            raise MappingError("A stop-loss limit order needs a limit price and a trigger.")
        book, price, trig = "SL", int(limit), int(trigger)
    elif otype == "SL-M":
        if not trigger:
            raise MappingError("A stop-loss market order needs a trigger.")
        book, price, trig = "SL", int(protection or 0), int(trigger)
    else:
        raise MappingError(f"Order type {otype!r} has no 021 equivalent.")
    return {
        "exchange": req_exch,
        "token": int(token),
        "qty": qty if side == "BUY" else -qty,
        "price": price,
        "book": book,
        "trigger": trig,
        "discQuantity": 0,
        "product": product,
        "validity": validity,
        "amo": False,
    }


def modify_body(current: dict, *, quantity: int | None = None, price: int | None = None, trigger: int | None = None) -> dict:
    """PUT /orders/{id} needs the FULL new state of the order. Built from the live 021 order (GET /orders/{id}) with
    only the requested fields changed; the side is preserved from the live order."""
    exch = REQUEST_EXCHANGE.get(current["exchange"], current["exchange"])
    remaining = int(current.get("qtyRemaining") or 0)
    traded = int(current.get("qtyTraded") or 0)
    sign = -1 if (remaining < 0 or (remaining == 0 and traded < 0)) else 1
    total = abs(remaining) + abs(traded)
    qty = sign * (quantity if quantity is not None else total)
    out = {
        "exchange": exch,
        "token": int(current["token"]),
        "qty": qty,
        "price": int(price if price is not None else current.get("price") or 0),
        "book": current.get("book") or "RL",
        "product": current["product"],
        "validity": current.get("validity") or "Day",
    }
    trig = trigger if trigger is not None else int(current.get("triggerPrice") or 0)
    if out["book"] == "SL" or trig:
        out["trigger"] = int(trig)
    return out


def fingerprint(exchange_021: str, token: int, qty: int, price: int, book: str, trigger: int, product: str, validity: str) -> str:
    """Identity of an order's economic content. Placements are serialized per account and a second placement with
    the same fingerprint is refused while one is unresolved, so a match in the order book is unambiguous."""
    return f"{exchange_021}|{int(token)}|{int(qty)}|{int(price)}|{book}|{int(trigger or 0)}|{product}|{validity}"


def request_fingerprint(o21: dict) -> str:
    return fingerprint(RESPONSE_EXCHANGE.get(o21["exchange"], o21["exchange"]), o21["token"], o21["qty"], o21["price"], o21["book"], o21.get("trigger") or 0, o21["product"], o21["validity"])


def signed_qty(o: dict) -> int | None:
    """Signed original quantity of a live 021 order, or None when the sign cannot be known from the row alone."""
    remaining = int(o.get("qtyRemaining") or 0)
    traded = int(o.get("qtyTraded") or 0)
    total = abs(remaining) + abs(traded)
    if remaining < 0 or traded < 0:
        return -total
    if remaining > 0:
        return total
    return None


def matches(o: dict, fp_fields: dict, not_before: float) -> bool:
    """Does a live 021 order row match a placement (same economic content, placed at or after not_before)?"""
    if int(o.get("time") or 0) < not_before:
        return False
    if o.get("exchange") != fp_fields["exchange"] or int(o.get("token") or 0) != int(fp_fields["token"]):
        return False
    q = signed_qty(o)
    total = abs(int(o.get("qtyRemaining") or 0)) + abs(int(o.get("qtyTraded") or 0))
    if total != abs(int(fp_fields["qty"])):
        return False
    if q is not None and (q > 0) != (int(fp_fields["qty"]) > 0):
        return False
    return (
        int(o.get("price") or 0) == int(fp_fields["price"])
        and (o.get("book") or "RL") == fp_fields["book"]
        and int(o.get("triggerPrice") or 0) == int(fp_fields.get("trigger") or 0)
        and o.get("product") == fp_fields["product"]
        and (o.get("validity") or "Day") == fp_fields["validity"]
    )


def order_status(o: dict) -> str:
    st = o.get("status") or ""
    if st in TERMINAL_021:
        return TERMINAL_021[st]
    traded = abs(int(o.get("qtyTraded") or 0))
    if traded > 0:
        return "PARTIALLY_FILLED"
    if (o.get("book") or "RL") == "SL" and st in ("Received", "Placed", "Pending", "Frozen", "AcceptedForAMO"):
        return "TRIGGER_PENDING"
    return "OPEN"


EXCHANGE_EPOCH_SHIFT = 315513000  # 021 trade times arrive shifted by this many seconds (verified against order times)


def unix_ts(ts: int | float | None) -> int | None:
    """021 order times are Unix seconds; trade times observed live are Unix seconds + 315513000. Values beyond 2033
    are treated as shifted and corrected."""
    if not ts:
        return None
    ts = int(ts)
    return ts - EXCHANGE_EPOCH_SHIFT if ts > 2_000_000_000 else ts


def _iso(ts: int | float | None) -> str | None:
    u = unix_ts(ts)
    if not u:
        return None
    return datetime.fromtimestamp(u, UTC).isoformat().replace("+00:00", "Z")


def order_json(o: dict, *, account_id: str, instrument: dict | None, attempt: dict | None = None, avg_fill: int | None = None, side_hint: str | None = None) -> dict:
    """A live 021 order row in the paper broker's _order_json shape, plus the raw 021 fields (suffix _021).

    instrument: {"instrument_id", "symbol", "segment"} when the token maps to a Countersign instrument.
    attempt: the gateway's placement record when Countersign placed it (gives the tag and the signed order type)."""
    req = (attempt or {}).get("request") or {}
    q = signed_qty(o)
    side = req.get("side") or (("BUY" if q > 0 else "SELL") if q is not None else None) or side_hint
    remaining = abs(int(o.get("qtyRemaining") or 0))
    traded = abs(int(o.get("qtyTraded") or 0))
    status = order_status(o)
    price = int(o.get("price") or 0)
    trig = int(o.get("triggerPrice") or 0)
    book = o.get("book") or "RL"
    otype = req.get("order_type") or ("SL" if book == "SL" and price else "SL-M" if book == "SL" else "LIMIT" if price else "MARKET")
    limit = price if otype in ("LIMIT", "SL") else None
    protection = price if otype in ("MARKET", "SL-M") and price else None
    exch = o.get("exchange") or ""
    inst = instrument or {}
    placed_by = o.get("placedBy") or ""
    return {
        "broker_order_id": str(o.get("orderId")),
        "client_order_id": (attempt or {}).get("tag"),
        "account_id": account_id,
        "instrument_id": inst.get("instrument_id") or f"021:{exch}:{o.get('token')}",
        "symbol": inst.get("symbol") or str(o.get("symbol") or o.get("token")),
        "segment": inst.get("segment") or SEGMENT_OF.get(exch, "EQ"),
        "side": side or "BUY",  # only when 021 gives no sign anywhere (no remaining quantity and no fills): see side_known
        "side_known": side is not None,
        "quantity": remaining + traded,
        "filled_quantity": traded,
        "pending_quantity": remaining if status in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED") else 0,
        "avg_fill_price_paise": avg_fill if traded else None,
        "order_type": otype,
        "product": PRODUCT_FROM_021.get(o.get("product") or "", o.get("product")),
        "validity": VALIDITY_FROM_021.get(o.get("validity") or "", o.get("validity")),
        "limit_price_paise": limit,
        "trigger_price_paise": trig or None,
        "protection_price_paise": protection,
        "status": status,
        "reject_reason": o.get("reason") or None,
        "source": "api" if attempt else ("rms" if placed_by == "RMS" else "external"),
        "origin": "countersign" if attempt else "external",  # external: in 021's book but not placed by Countersign
        "placed_at": _iso(o.get("time")),
        "updated_at": _iso(o.get("lastActivity") or o.get("time")),
        "broker": "021",
        "status_021": o.get("status"),
        "placed_by_021": placed_by or None,
        "price_021_paise": price,
        "trigger_021_paise": trig,
        "qty_021": q,
        "book_021": book,
        "exchange_021": exch,
        "token_021": o.get("token"),
        "exchange_order_number": o.get("exchangeOrderNumber") or None,
    }


def avg_price(trades: list[dict]) -> int | None:
    qty = sum(abs(int(t.get("quantity") or 0)) for t in trades)
    if not qty:
        return None
    return sum(abs(int(t.get("quantity") or 0)) * int(t.get("price") or 0) for t in trades) // qty


def realised_by_position(trades: list[dict]) -> dict[tuple[str, int, str], int]:
    """Realised P&L today per (exchange, token, product) from today's trades: closed quantity x (avg sell - avg buy)."""
    agg: dict[tuple[str, int, str], list[int]] = {}
    for t in trades:
        k = (t.get("exchange") or "", int(t.get("token") or 0), t.get("product") or "")
        q = int(t.get("quantity") or 0)
        px = int(t.get("price") or 0)
        a = agg.setdefault(k, [0, 0, 0, 0])  # bought qty, bought value, sold qty, sold value
        if q > 0:
            a[0] += q
            a[1] += q * px
        elif q < 0:
            a[2] += -q
            a[3] += -q * px
    out = {}
    for k, (bq, bv, sq, sv) in agg.items():
        closed = min(bq, sq)
        out[k] = closed * (sv // sq - bv // bq) if closed else 0
    return out
