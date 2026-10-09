"""Paper broker: an independent process with its own database that behaves like an external broker.

- Key scopes: read keys cannot place orders; write keys can; the naive key is confined to NAIVE1. The read and write
  keys cover every trader account (DEMO1, LAB1, each provisioned PT account), resolved per request from b_accounts,
  so a restart never drops a provisioned account; they can never touch NAIVE1.
- Fills come from live ticks (limit orders fill when marketable; market orders must carry price protection).
- Deterministic fault injection for demos: accept_then_drop, delay_ack, reject, partial_fill, connection_failure.
- Like real Indian broker APIs, it does NOT deduplicate client tags: duplicate prevention is the client's job."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import UTC, datetime
from functools import lru_cache
from itertools import count

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import BigInteger, Integer, String, Text, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from countersign.core import events
from countersign.core.config import settings
from countersign.core.market import Tick

STARTING_CASH = 25_00_000_00  # Rs 25 lakh in paise


class B(DeclarativeBase):
    pass


class BAccount(B):
    __tablename__ = "b_accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    cash_paise: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16), default="trader", server_default="trader")  # trader | lab | naive


class BKey(B):
    __tablename__ = "b_keys"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))
    accounts: Mapped[str] = mapped_column(Text)  # comma separated, "*" (admin/audit) or "@trading" (every non-naive account)


class BHolding(B):
    __tablename__ = "b_holdings"
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    avg_price_paise: Mapped[int] = mapped_column(BigInteger)


class BPosition(B):
    __tablename__ = "b_positions"
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product: Mapped[str] = mapped_column(String(8), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    avg_price_paise: Mapped[int] = mapped_column(BigInteger)
    realised_paise: Mapped[int] = mapped_column(BigInteger, default=0)


class BOrder(B):
    __tablename__ = "b_orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    tag: Mapped[str | None] = mapped_column(String(20), index=True)
    instrument_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    segment: Mapped[str] = mapped_column(String(4))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    order_type: Mapped[str] = mapped_column(String(8))
    product: Mapped[str] = mapped_column(String(8))
    validity: Mapped[str] = mapped_column(String(4))
    limit_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    trigger_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    protection_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="api")
    placed_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


class BTrade(B):
    __tablename__ = "b_trades"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    price_paise: Mapped[int] = mapped_column(BigInteger)
    ts: Mapped[str] = mapped_column(String(40))


@lru_cache
def _engine():
    return create_engine(settings().broker_database_url, pool_pre_ping=True, pool_size=10, future=True)


@lru_cache
def _sf():
    return sessionmaker(bind=_engine(), expire_on_commit=False)


_lock = threading.RLock()
_order_seq = count(int(time.time()) % 100000)
_fault = {"mode": "none", "remaining": 0}

SEED_HOLDINGS = {
    "DEMO1": [
        ("NSE:EQ:RELIANCE", "RELIANCE", 50, 275000),
        ("NSE:EQ:INFY", "INFY", 100, 182040),
        ("NSE:EQ:TCS", "TCS", 20, 398560),
        ("NSE:EQ:HDFCBANK", "HDFCBANK", 60, 169025),
        ("NSE:EQ:TATAMOTORS", "TATAMOTORS", 200, 97855),
        ("NSE:EQ:SBIN", "SBIN", 150, 86020),
        ("NSE:EQ:ITC", "ITC", 300, 44575),
    ],
    "LAB1": [("NSE:EQ:INFY", "INFY", 100, 182040), ("NSE:EQ:ABCCORP", "ABCCORP", 50, 49800)],
    "NAIVE1": [],
}
SEED_POSITIONS = {"DEMO1": [("NSE:EQ:ICICIBANK", "ICICIBANK", "MIS", 40, 129880)]}
TEMPLATE_ACCOUNT = "DEMO1"  # every provisioned paper account starts exactly like the demo account
TRADING = "@trading"
PAPER_ACCOUNT_RE = re.compile(r"PT[A-Z2-7]{6}")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _hash(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def _kind(account_id: str) -> str:
    if account_id == settings().naive_account_id:
        return "naive"
    return "lab" if account_id == "LAB1" else "trader"


def _starting_cash(account_id: str) -> int:
    return 50_00_000_00 if _kind(account_id) == "naive" else STARTING_CASH


def _register_keys(s: Session) -> None:
    cfg = settings()
    for key, scope, accounts in (
        (cfg.broker_read_key, "read", TRADING),
        (cfg.broker_write_key, "write", TRADING),
        (cfg.broker_admin_key, "admin", "*"),
        (cfg.broker_naive_key, "naive", cfg.naive_account_id),
        (cfg.broker_audit_key, "audit", "*"),
    ):
        if key:
            s.merge(BKey(key_hash=_hash(key), scope=scope, accounts=accounts))


def _seed_avg(instrument_id: str, avg: int) -> int:
    """Seed averages are written against the fictional seed prices. When the 021 gateway prices an instrument live,
    the average keeps the same ratio to today's real previous close, so a paper account's P&L stays plausible."""
    from countersign.core.instruments import EQUITIES

    ref = next((int(round(close * 100)) for sym, _n, _i, close, _t, _a in EQUITIES if f"NSE:EQ:{sym}" == instrument_id), None)
    try:
        raw = events.client().get(events.TICK_KEY.format(instrument_id))
        live = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        live = None
    if not ref or not live or live.get("src") != "021" or not live.get("prev_close_paise"):
        return avg
    out = int(live["prev_close_paise"]) * avg // ref
    return max(5, out - out % 5)


def seed_account(s: Session, account_id: str) -> None:
    """(Re)seeds one account's cash, holdings and positions from its template. Orders and trades are untouched."""
    template = account_id if account_id in SEED_HOLDINGS else TEMPLATE_ACCOUNT
    s.query(BHolding).filter(BHolding.account_id == account_id).delete()
    s.query(BPosition).filter(BPosition.account_id == account_id).delete()
    s.merge(BAccount(id=account_id, cash_paise=_starting_cash(account_id), kind=_kind(account_id)))
    for iid, sym, q, avg in SEED_HOLDINGS[template]:
        s.add(BHolding(account_id=account_id, instrument_id=iid, symbol=sym, quantity=q, avg_price_paise=_seed_avg(iid, avg)))
    for iid, sym, prod, q, avg in SEED_POSITIONS.get(template, []):
        s.add(BPosition(account_id=account_id, instrument_id=iid, product=prod, symbol=sym, quantity=q, avg_price_paise=_seed_avg(iid, avg)))


def seed(s: Session, hard: bool = True) -> None:
    ids = set(SEED_HOLDINGS) | set(s.execute(select(BAccount.id)).scalars())
    if hard:
        s.query(BTrade).delete()
        s.query(BOrder).delete()
    for acc in sorted(ids):
        seed_account(s, acc)


def _migrate(conn) -> None:
    has_kind = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='b_accounts' AND column_name='kind'")).first()
    if has_kind is None:
        conn.execute(text("ALTER TABLE b_accounts ADD COLUMN IF NOT EXISTS kind varchar(16) NOT NULL DEFAULT 'trader'"))
    conn.execute(text("UPDATE b_accounts SET kind='lab' WHERE id='LAB1' AND kind<>'lab'"))
    conn.execute(text("UPDATE b_accounts SET kind='naive' WHERE id=:n AND kind<>'naive'"), {"n": settings().naive_account_id})


def init_db() -> None:
    B.metadata.create_all(_engine())
    with _engine().begin() as c:
        _migrate(c)
    with _sf()() as s:
        _register_keys(s)
        if s.get(BAccount, TEMPLATE_ACCOUNT) is None:
            seed(s)
        for acc in SEED_HOLDINGS:
            if s.get(BAccount, acc) is None:
                seed_account(s, acc)
        s.commit()


# ------------------------------------------------------------------ auth


class Caller(BaseModel):
    scope: str
    accounts: list[str]
    account_id: str


def key_allows(accounts: str, account_id: str, kind: str | None) -> bool:
    """Scope rule for one key: "*" any account, "@trading" any existing non-naive account, else an explicit list."""
    if accounts == "*":
        return True
    if accounts == TRADING:
        return kind is not None and kind != "naive" and account_id != settings().naive_account_id
    return account_id in accounts.split(",")


def caller(authorization: str = Header(default=""), x_account: str = Header(default="")) -> Caller:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Missing broker API key."})
    with _sf()() as s:
        k = s.get(BKey, _hash(authorization[7:]))
        if k is None:
            raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Invalid broker API key."})
        if k.accounts == TRADING and not x_account:
            raise HTTPException(400, {"code": "ACCOUNT_REQUIRED", "message": "This key covers several accounts; send X-Account."})
        default = "DEMO1" if k.accounts == "*" else k.accounts.split(",")[0]
        account = x_account or default
        acc = s.get(BAccount, account)
        kind = acc.kind if acc is not None else None
    if not key_allows(k.accounts, account, kind):
        raise HTTPException(403, {"code": "ACCOUNT_FORBIDDEN", "message": f"This key cannot access account {account}."})
    return Caller(scope=k.scope, accounts=k.accounts.split(","), account_id=account)


def need(*scopes: str):
    def dep(c: Caller = Depends(caller)) -> Caller:
        if c.scope not in scopes:
            raise HTTPException(403, {"code": "SCOPE_FORBIDDEN", "message": f"Key scope '{c.scope}' cannot perform this operation."})
        return c

    return dep


app = FastAPI(title="Countersign paper broker")


@app.exception_handler(HTTPException)
def _http_exc(_: Request, exc: HTTPException):
    d = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(d, status_code=exc.status_code)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    threading.Thread(target=_tick_loop, daemon=True, name="broker-ticks").start()
    threading.Thread(target=_housekeeping, daemon=True, name="broker-housekeeping").start()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "fault": _fault}


# ------------------------------------------------------------------ helpers


def _order_json(o: BOrder) -> dict:
    return {
        "broker_order_id": o.id,
        "client_order_id": o.tag,
        "account_id": o.account_id,
        "instrument_id": o.instrument_id,
        "symbol": o.symbol,
        "segment": o.segment,
        "side": o.side,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "pending_quantity": o.quantity - o.filled_quantity if o.status in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED") else 0,
        "avg_fill_price_paise": o.avg_fill_price_paise,
        "order_type": o.order_type,
        "product": o.product,
        "validity": o.validity,
        "limit_price_paise": o.limit_price_paise,
        "trigger_price_paise": o.trigger_price_paise,
        "protection_price_paise": o.protection_price_paise,
        "status": o.status,
        "reject_reason": o.reject_reason,
        "source": o.source,
        "placed_at": o.placed_at,
        "updated_at": o.updated_at,
    }


def _publish(o: BOrder) -> None:
    events.publish(events.CH_BROKER_UPDATES, {"order": _order_json(o)})


def _tick(instrument_id: str) -> Tick | None:
    raw = events.client().get(events.TICK_KEY.format(instrument_id))
    return Tick.from_json(raw) if raw else None


def _reserved(s: Session, account_id: str) -> int:
    tot = 0
    for o in s.execute(select(BOrder).where(BOrder.account_id == account_id, BOrder.status.in_(["OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED"]), BOrder.side == "BUY")).scalars():
        px = o.limit_price_paise or o.protection_price_paise or 0
        tot += (o.quantity - o.filled_quantity) * px
    return tot


def _apply_fill(s: Session, o: BOrder, qty: int, price: int) -> None:
    prev_val = (o.avg_fill_price_paise or 0) * o.filled_quantity
    o.filled_quantity += qty
    o.avg_fill_price_paise = (prev_val + qty * price) // o.filled_quantity
    o.status = "COMPLETE" if o.filled_quantity >= o.quantity else "PARTIALLY_FILLED"
    o.updated_at = _now()
    s.add(BTrade(id=f"T{o.id}-{o.filled_quantity}", order_id=o.id, account_id=o.account_id, quantity=qty, price_paise=price, ts=_now()))
    acc = s.get(BAccount, o.account_id)
    signed = qty if o.side == "BUY" else -qty
    if o.product == "CNC" and o.segment == "EQ":
        h = s.get(BHolding, (o.account_id, o.instrument_id))
        if o.side == "BUY":
            acc.cash_paise -= qty * price
            if h is None:
                s.add(BHolding(account_id=o.account_id, instrument_id=o.instrument_id, symbol=o.symbol, quantity=qty, avg_price_paise=price))
            else:
                h.avg_price_paise = (h.avg_price_paise * h.quantity + price * qty) // (h.quantity + qty)
                h.quantity += qty
        else:
            acc.cash_paise += qty * price
            if h is not None:
                h.quantity -= qty
                if h.quantity <= 0:
                    s.delete(h)
    else:
        p = s.get(BPosition, (o.account_id, o.instrument_id, o.product))
        acc.cash_paise -= signed * price
        if p is None:
            s.add(BPosition(account_id=o.account_id, instrument_id=o.instrument_id, product=o.product, symbol=o.symbol, quantity=signed, avg_price_paise=price))
        else:
            newq = p.quantity + signed
            if p.quantity == 0 or (p.quantity > 0) == (signed > 0):
                p.avg_price_paise = (p.avg_price_paise * abs(p.quantity) + price * qty) // max(abs(newq), 1)
            else:
                closed = min(abs(signed), abs(p.quantity))
                p.realised_paise += closed * (price - p.avg_price_paise) * (1 if p.quantity > 0 else -1)
            p.quantity = newq


def _try_fill(s: Session, o: BOrder, tick: Tick) -> bool:
    if o.status not in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED"):
        return False
    if o.status == "TRIGGER_PENDING":
        trig = o.trigger_price_paise or 0
        hit = tick.ltp_paise >= trig if o.side == "BUY" else tick.ltp_paise <= trig
        if not hit:
            return False
        o.status = "OPEN"
    cap = o.limit_price_paise if o.order_type in ("LIMIT", "SL") else o.protection_price_paise
    marketable = tick.ltp_paise <= cap if o.side == "BUY" else tick.ltp_paise >= cap
    if not marketable:
        return False
    remaining = o.quantity - o.filled_quantity
    _apply_fill(s, o, remaining, tick.ltp_paise)
    return True


# ------------------------------------------------------------------ read endpoints


@app.get("/v1/funds")
def funds(c: Caller = Depends(need("read", "write", "audit"))) -> dict:
    with _sf()() as s:
        acc = s.get(BAccount, c.account_id)
        if acc is None:
            raise HTTPException(404, {"code": "ACCOUNT_NOT_FOUND", "message": f"No account {c.account_id}."})
        reserved = _reserved(s, c.account_id)
        mis_margin = sum(abs(p.quantity) * p.avg_price_paise // 5 for p in s.execute(select(BPosition).where(BPosition.account_id == c.account_id)).scalars())
        used = reserved + mis_margin
        return {"account_id": c.account_id, "cash_paise": acc.cash_paise, "used_margin_paise": used, "available_paise": acc.cash_paise - used}


@app.get("/v1/holdings")
def holdings(c: Caller = Depends(need("read", "write", "audit"))) -> list[dict]:
    with _sf()() as s:
        rows = s.execute(select(BHolding).where(BHolding.account_id == c.account_id, BHolding.quantity > 0)).scalars().all()
        return [{"instrument_id": h.instrument_id, "symbol": h.symbol, "quantity": h.quantity, "avg_price_paise": h.avg_price_paise} for h in rows]


@app.get("/v1/positions")
def positions(c: Caller = Depends(need("read", "write", "audit"))) -> list[dict]:
    with _sf()() as s:
        rows = s.execute(select(BPosition).where(BPosition.account_id == c.account_id)).scalars().all()
        return [
            {"instrument_id": p.instrument_id, "symbol": p.symbol, "product": p.product, "quantity": p.quantity, "avg_price_paise": p.avg_price_paise, "realised_paise": p.realised_paise}
            for p in rows
            if p.quantity != 0 or p.realised_paise != 0
        ]


@app.get("/v1/orders")
def orders(status: str = "all", tag: str | None = None, c: Caller = Depends(need("read", "write", "audit", "naive"))) -> list[dict]:
    with _sf()() as s:
        q = select(BOrder).where(BOrder.account_id == c.account_id)
        if tag:
            q = q.where(BOrder.tag == tag)
        if status == "open":
            q = q.where(BOrder.status.in_(["OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED"]))
        elif status == "rejected":
            q = q.where(BOrder.status == "REJECTED")
        return [_order_json(o) for o in s.execute(q.order_by(BOrder.placed_at.desc())).scalars()]


@app.get("/v1/orders/{oid}")
def order(oid: str, c: Caller = Depends(need("read", "write", "audit"))) -> dict:
    with _sf()() as s:
        o = s.get(BOrder, oid)
        if o is None or o.account_id != c.account_id:
            raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": f"No order {oid}."})
        return _order_json(o)


@app.get("/v1/trades")
def trades(c: Caller = Depends(need("read", "write", "audit"))) -> list[dict]:
    with _sf()() as s:
        return [
            {"trade_id": t.id, "broker_order_id": t.order_id, "quantity": t.quantity, "price_paise": t.price_paise, "ts": t.ts}
            for t in s.execute(select(BTrade).where(BTrade.account_id == c.account_id)).scalars()
        ]


# ------------------------------------------------------------------ write endpoints


class PlaceBody(BaseModel):
    instrument_id: str
    symbol: str
    segment: str = "EQ"
    side: str
    quantity: int
    order_type: str
    product: str
    validity: str = "DAY"
    limit_price_paise: int | None = None
    trigger_price_paise: int | None = None
    protection_price_paise: int | None = None
    tag: str | None = None


def _take_fault() -> str:
    with _lock:
        if _fault["mode"] != "none" and _fault["remaining"] > 0:
            _fault["remaining"] -= 1
            mode = _fault["mode"]
            if _fault["remaining"] == 0:
                _fault["mode"] = "none"
            return mode
    return "none"


def _place(body: PlaceBody, account_id: str, source: str, fault: str = "none") -> dict:
    if fault == "connection_failure":
        raise HTTPException(503, {"code": "NOT_ACCEPTED", "message": "Broker gateway unavailable; order was NOT accepted (simulated connection failure)."})
    if body.order_type in ("MARKET", "SL-M") and not body.protection_price_paise:
        raise HTTPException(422, {"code": "MARKET_PROTECTION_REQUIRED", "message": "Market orders must carry market price protection."})
    if body.quantity <= 0:
        raise HTTPException(422, {"code": "INVALID_QUANTITY", "message": "Quantity must be positive."})
    with _lock, _sf()() as s:
        oid = f"2610{next(_order_seq):08d}"
        o = BOrder(
            id=oid, account_id=account_id, tag=body.tag, instrument_id=body.instrument_id, symbol=body.symbol, segment=body.segment,
            side=body.side, quantity=body.quantity, order_type=body.order_type, product=body.product, validity=body.validity,
            limit_price_paise=body.limit_price_paise, trigger_price_paise=body.trigger_price_paise,
            protection_price_paise=body.protection_price_paise, status="OPEN", source=source, placed_at=_now(), updated_at=_now(),
        )
        if body.order_type in ("SL", "SL-M"):
            o.status = "TRIGGER_PENDING"
        tick = _tick(body.instrument_id)
        if fault == "reject":
            o.status, o.reject_reason = "REJECTED", "RMS: order rejected by risk check (simulated broker rejection)"
        elif body.side == "SELL" and body.product == "CNC" and body.segment == "EQ":
            h = s.get(BHolding, (account_id, body.instrument_id))
            pending = sum(x.quantity - x.filled_quantity for x in s.execute(select(BOrder).where(BOrder.account_id == account_id, BOrder.instrument_id == body.instrument_id, BOrder.side == "SELL", BOrder.status.in_(["OPEN", "PARTIALLY_FILLED"]))).scalars())
            if h is None or h.quantity - pending < body.quantity:
                o.status, o.reject_reason = "REJECTED", "RMS: insufficient holdings for delivery sell"
        elif body.side == "BUY":
            acc = s.get(BAccount, account_id)
            px = body.limit_price_paise or body.protection_price_paise or (tick.ltp_paise if tick else 0)
            if body.quantity * px > acc.cash_paise - _reserved(s, account_id):
                o.status, o.reject_reason = "REJECTED", "RMS: insufficient funds"
        s.add(o)
        s.flush()
        if o.status in ("OPEN", "TRIGGER_PENDING") and tick is not None:
            if fault == "partial_fill":
                cap = o.limit_price_paise or o.protection_price_paise
                if o.status == "OPEN" and (tick.ltp_paise <= cap if o.side == "BUY" else tick.ltp_paise >= cap):
                    part = max(1, o.quantity * 4 // 10)
                    _apply_fill(s, o, part, tick.ltp_paise)
                    o.reject_reason = "Partial fill: only part of the quantity was available at your price (simulated thin liquidity)"
                    _partial_cancel_later.append((time.time() + 3.0, o.id))
            else:
                _try_fill(s, o, tick)
        s.commit()
        out = _order_json(o)
        _publish(o)
    return out


_partial_cancel_later: list[tuple[float, str]] = []


@app.post("/v1/orders")
def place(body: PlaceBody, c: Caller = Depends(need("write", "naive"))) -> dict:
    fault = _take_fault() if c.scope in ("write", "naive") else "none"
    if fault == "delay_ack":
        out = _place(body, c.account_id, "api")
        time.sleep(1.0)
        return out
    if fault == "accept_then_drop":
        _place(body, c.account_id, "api")
        time.sleep(settings().executor_broker_timeout_s + 2.5)
        raise HTTPException(504, {"code": "GATEWAY_TIMEOUT", "message": "Response lost after acceptance (simulated)."})
    return _place(body, c.account_id, "naive" if c.scope == "naive" else "api", fault)


class ModifyBody(BaseModel):
    quantity: int | None = None
    limit_price_paise: int | None = None
    trigger_price_paise: int | None = None


@app.put("/v1/orders/{oid}")
def modify(oid: str, body: ModifyBody, c: Caller = Depends(need("write"))) -> dict:
    with _lock, _sf()() as s:
        o = s.get(BOrder, oid)
        if o is None or o.account_id != c.account_id:
            raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": f"No order {oid}."})
        if o.status not in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED"):
            raise HTTPException(409, {"code": "ORDER_NOT_OPEN", "message": f"Order {oid} is {o.status} and cannot be modified."})
        if body.quantity is not None:
            o.quantity = body.quantity
        if body.limit_price_paise is not None:
            o.limit_price_paise = body.limit_price_paise
        if body.trigger_price_paise is not None:
            o.trigger_price_paise = body.trigger_price_paise
        o.updated_at = _now()
        tick = _tick(o.instrument_id)
        if tick:
            _try_fill(s, o, tick)
        s.commit()
        _publish(o)
        return _order_json(o)


@app.delete("/v1/orders/{oid}")
def cancel(oid: str, c: Caller = Depends(need("write"))) -> dict:
    with _lock, _sf()() as s:
        o = s.get(BOrder, oid)
        if o is None or o.account_id != c.account_id:
            raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": f"No order {oid}."})
        if o.status not in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED"):
            raise HTTPException(409, {"code": "ORDER_NOT_OPEN", "message": f"Order {oid} is {o.status} and cannot be cancelled."})
        o.status = "CANCELLED"
        o.updated_at = _now()
        s.commit()
        _publish(o)
        return _order_json(o)


# ------------------------------------------------------------------ admin (demo controls, monitor)


class FaultBody(BaseModel):
    mode: str
    count: int = 1


@app.post("/admin/faults")
def set_fault(body: FaultBody, c: Caller = Depends(need("admin"))) -> dict:
    allowed = {"none", "accept_then_drop", "delay_ack", "reject", "partial_fill", "connection_failure"}
    if body.mode not in allowed:
        raise HTTPException(422, {"code": "UNKNOWN_FAULT", "message": f"Fault must be one of {sorted(allowed)}."})
    with _lock:
        _fault["mode"] = body.mode
        _fault["remaining"] = 0 if body.mode == "none" else min(max(1, body.count), 5)  # a drill, never a standing outage
    return dict(_fault)


@app.get("/admin/faults")
def get_fault(c: Caller = Depends(need("admin"))) -> dict:
    return dict(_fault)


@app.post("/admin/reset")
def reset(c: Caller = Depends(need("admin"))) -> dict:
    with _lock, _sf()() as s:
        seed(s, hard=True)
        s.commit()
        _fault.update({"mode": "none", "remaining": 0})
    return {"ok": True}


@app.post("/admin/replenish/{account_id}")
def replenish(account_id: str, c: Caller = Depends(need("admin"))) -> dict:
    """Restores a sandbox account's seed cash, holdings and positions and expires its resting orders, keeping the
    order history (the monitor still checks every order). Used by the Attack Lab so repeated runs never drain LAB1."""
    if account_id not in ("LAB1", "NAIVE1"):
        raise HTTPException(403, {"code": "NOT_A_SANDBOX", "message": "Only the LAB1 and NAIVE1 sandbox accounts can be replenished."})
    with _lock, _sf()() as s:
        expired = []
        for o in s.execute(select(BOrder).where(BOrder.account_id == account_id, BOrder.status.in_(["OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED"]))).scalars():
            o.status = "CANCELLED"
            o.reject_reason = "Expired: sandbox replenished for a new Attack Lab run"
            o.updated_at = _now()
            expired.append(o)
        s.query(BHolding).filter(BHolding.account_id == account_id).delete()
        s.query(BPosition).filter(BPosition.account_id == account_id).delete()
        s.merge(BAccount(id=account_id, cash_paise=_starting_cash(account_id), kind=_kind(account_id)))
        for iid, sym, q, avg in SEED_HOLDINGS.get(account_id, []):
            s.add(BHolding(account_id=account_id, instrument_id=iid, symbol=sym, quantity=q, avg_price_paise=avg))
        s.commit()
        for o in expired:
            _publish(o)
    return {"ok": True, "account_id": account_id, "orders_expired": len(expired)}


class NewAccountBody(BaseModel):
    account_id: str


@app.post("/admin/accounts", status_code=201)
def create_account(body: NewAccountBody, c: Caller = Depends(need("admin"))) -> dict:
    """Provisions a new paper trading account for a signed-up trader, seeded exactly like DEMO1."""
    if not PAPER_ACCOUNT_RE.fullmatch(body.account_id):
        raise HTTPException(422, {"code": "INVALID_ACCOUNT_ID", "message": "Paper account ids are PT followed by 6 base32 characters."})
    with _lock, _sf()() as s:
        if s.get(BAccount, body.account_id) is not None:
            raise HTTPException(409, {"code": "ACCOUNT_EXISTS", "message": f"Account {body.account_id} already exists."})
        seed_account(s, body.account_id)
        s.commit()
    return {"ok": True, "account_id": body.account_id, "cash_paise": STARTING_CASH}


@app.post("/admin/accounts/{account_id}/reset")
def reset_account(account_id: str, c: Caller = Depends(need("admin"))) -> dict:
    """Hard reset of ONE trader account (per-user demo reset): its orders and trades are deleted and cash, holdings
    and position reseeded. Sandboxes are replenished instead, so the monitor keeps their full order history."""
    with _lock, _sf()() as s:
        acc = s.get(BAccount, account_id)
        if acc is None:
            raise HTTPException(404, {"code": "ACCOUNT_NOT_FOUND", "message": f"No account {account_id}."})
        if acc.kind != "trader":
            raise HTTPException(403, {"code": "NOT_A_TRADER_ACCOUNT", "message": "Sandbox accounts are replenished, not reset."})
        s.query(BTrade).filter(BTrade.account_id == account_id).delete()
        deleted = s.query(BOrder).filter(BOrder.account_id == account_id).delete()
        seed_account(s, account_id)
        s.commit()
    return {"ok": True, "account_id": account_id, "orders_deleted": deleted}


@app.get("/admin/orders")
def all_orders(c: Caller = Depends(need("admin", "audit"))) -> list[dict]:
    with _sf()() as s:
        return [_order_json(o) for o in s.execute(select(BOrder).order_by(BOrder.placed_at)).scalars()]


class RogueBody(PlaceBody):
    account_id: str


@app.post("/admin/rogue-order")
def rogue(body: RogueBody, c: Caller = Depends(need("admin"))) -> dict:
    """Simulates an out-of-band order (e.g. stolen broker credentials) so the independent monitor can be demoed."""
    pb = PlaceBody(**body.model_dump(exclude={"account_id"}))
    return _place(pb, body.account_id, "rogue")


# ------------------------------------------------------------------ background loops


def _tick_loop() -> None:
    while True:
        try:
            ps = events.client().pubsub(ignore_subscribe_messages=True)
            ps.subscribe(events.CH_TICKS)
            for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                tick = Tick.from_json(msg["data"])
                with _lock, _sf()() as s:
                    rows = s.execute(select(BOrder).where(BOrder.instrument_id == tick.instrument_id, BOrder.status.in_(["OPEN", "TRIGGER_PENDING"]))).scalars().all()
                    changed = [o for o in rows if _try_fill(s, o, tick)]
                    s.commit()
                    for o in changed:
                        _publish(o)
        except Exception:
            time.sleep(1.0)


def _housekeeping() -> None:
    while True:
        time.sleep(0.5)
        now = time.time()
        due = [x for x in _partial_cancel_later if x[0] <= now]
        for item in due:
            _partial_cancel_later.remove(item)
            with _lock, _sf()() as s:
                o = s.get(BOrder, item[1])
                if o and o.status == "PARTIALLY_FILLED":
                    o.status = "CANCELLED"
                    o.reject_reason = f"Partially filled {o.filled_quantity}/{o.quantity}; remaining quantity cancelled by exchange (simulated thin liquidity)"
                    o.updated_at = _now()
                    s.commit()
                    _publish(o)


def main() -> None:
    import uvicorn

    uvicorn.run("countersign.broker.app:app", host="127.0.0.1", port=8100, log_level="warning")


if __name__ == "__main__":
    main()
