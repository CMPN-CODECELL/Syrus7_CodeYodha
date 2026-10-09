"""Shared helpers for the L2 to L4 tests (no tests here): a configurable read gateway, instrument prices taken from
the instrument master (so the tests do not depend on seeded numbers), and small builders."""

from __future__ import annotations

from countersign.core import proposals
from countersign.core.db import session_scope
from countersign.core.models import Instrument, Proposal
from countersign.core.verifier import AccountState
from tests.conftest import set_tick

ACCOUNT = "LAB1"


class Gw:
    """ReadGateway stand-in with holdings, positions and open orders in the broker's /v1 shapes."""

    def __init__(self, holdings=(), positions=(), orders=(), available_paise: int = 25_00_000_00):
        self._h = [dict(x) for x in holdings]
        self._p = [dict(x) for x in positions]
        self._o = [dict(x) for x in orders]
        self.available = available_paise

    def funds(self):
        return {"cash_paise": self.available, "available_paise": self.available, "used_margin_paise": 0}

    def holdings(self):
        return [dict(h) for h in self._h]

    def positions(self):
        return [dict(p) for p in self._p]

    def orders(self, status="all", tag=None):
        rows = [dict(o) for o in self._o]
        if status == "open":
            rows = [o for o in rows if o["status"] in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED")]
        return rows


def inst(symbol: str) -> Instrument:
    with session_scope() as s:
        i = s.get(Instrument, f"NSE:EQ:{symbol}")
        assert i is not None, symbol
        s.expunge(i)
        return i


def ltp(symbol: str, *, offset_ticks: int = 0, prev_offset_ticks: int = 0) -> tuple[int, int]:
    """Publishes a fresh tick at the instrument's previous close (aligned to its tick) plus an offset; returns (ltp, tick)."""
    i = inst(symbol)
    t = i.tick_size_paise
    base = i.prev_close_paise - i.prev_close_paise % t
    price = base + offset_ticks * t
    set_tick(i.instrument_id, price, prev=base + prev_offset_ticks * t)
    return price, t


def rupees(paise: int) -> str:
    return f"{paise / 100:.2f}".rstrip("0").rstrip(".")


def holding(symbol: str, qty: int, avg_paise: int, **extra) -> dict:
    return {"instrument_id": f"NSE:EQ:{symbol}", "symbol": symbol, "quantity": qty, "avg_price_paise": avg_paise, **extra}


def position(symbol: str, product: str, qty: int, avg_paise: int, **extra) -> dict:
    return {"instrument_id": f"NSE:EQ:{symbol}", "symbol": symbol, "product": product, "quantity": qty, "avg_price_paise": avg_paise, "realised_paise": 0, **extra}


def open_order(oid: str, symbol: str, side: str, qty: int, order_type: str, *, limit=None, trigger=None, product="CNC", status=None) -> dict:
    return {
        "broker_order_id": oid,
        "client_order_id": None,
        "instrument_id": f"NSE:EQ:{symbol}",
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "filled_quantity": 0,
        "pending_quantity": qty,
        "order_type": order_type,
        "product": product,
        "validity": "DAY",
        "limit_price_paise": limit,
        "trigger_price_paise": trigger,
        "protection_price_paise": None,
        "status": status or ("TRIGGER_PENDING" if order_type in ("SL", "SL-M") else "OPEN"),
    }


def account(gw: Gw) -> AccountState:
    return AccountState.load(gw, ACCOUNT)


def user_id() -> str:
    from tests.helpers import lab_user_id

    return lab_user_id()


def draft_order(text: str, gw: Gw, intent=None, engine: str = "deterministic") -> proposals.Outcome:
    """Builds through the real proposal service, from the parser's intent unless an AI intent is given."""
    from countersign.core.parser import parse

    p = parse(text)
    if intent is None:
        assert p.order is not None, (p.kind, p.question)
        intent = p.order
    with session_scope() as s:
        out = proposals.create_order(s, intent, user_text=text, account=account(gw), user_id=user_id(), engine=engine, origin="test", facts=p.facts if engine == "deterministic" else None)
        if out.proposal is not None:
            s.expunge(out.proposal)
        return out


def view(pid: str) -> dict:
    with session_scope() as s:
        return proposals.view(s, s.get(Proposal, pid))


def codes(v: dict) -> set[str]:
    return {i["code"] for i in v["issues"]}


def fields(v: dict, step: int = 0) -> dict[str, dict]:
    return {f["key"]: f for f in v["steps"][step]["fields"]}


def proposal_count() -> int:
    from sqlalchemy import func, select

    with session_scope() as s:
        return s.execute(select(func.count()).select_from(Proposal)).scalar_one()
