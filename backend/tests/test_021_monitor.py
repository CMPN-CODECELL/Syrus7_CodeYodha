"""The invariant monitor on the 021 account: orders Countersign did not place are informational (no kill switch)
unless CS_021_STRICT_MONITOR=true; a Countersign-tagged order must still map to a consumed, verified capability."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from countersign.core import state
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import SecurityEvent
from countersign.monitor.main import Monitor


class EmptyPaper:
    def all_orders(self):
        return []


class Admin021:
    def __init__(self, orders):
        self.orders = orders

    def all_orders(self):
        return self.orders


def _o(oid: str, tag=None, source="external", placed_by="User") -> dict:
    return {"broker_order_id": oid, "client_order_id": tag, "account_id": "HACK0000", "instrument_id": "NSE:EQ:IDEA", "symbol": "IDEA", "side": "BUY", "quantity": 1, "filled_quantity": 0, "limit_price_paise": 1187, "status": "CANCELLED", "status_021": "Cancelled", "source": source, "placed_by_021": placed_by, "broker": "021", "price_021_paise": 1187}


@pytest.fixture(autouse=True)
def _no_api(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no api in tests")))


def _events(kind: str) -> list[SecurityEvent]:
    with session_scope() as s:
        return list(s.execute(select(SecurityEvent).where(SecurityEvent.kind == kind)).scalars())


def test_orders_placed_outside_countersign_are_informational():
    m = Monitor(admin=EmptyPaper(), admin021=Admin021([_o("256159"), _o("256170", source="rms", placed_by="RMS")]))
    st = m.run_once()
    m.run_once()  # recorded once, not on every pass
    ev = _events("EXTERNAL_021_ORDER")
    assert len(ev) == 2 and all(e.severity == "info" and e.resolved for e in ev)
    assert any("RMS" in e.message for e in ev)
    assert st["external_orders_detected"] == 2 and st["violations"] == 0
    with session_scope() as s:
        assert not state.kill_switch(s)["engaged"]
    assert any(c["name"] == "021 orders placed outside Countersign" and c["ok"] for c in st["checks"])
    from countersign.core.models import OutcomeRow

    with session_scope() as s:
        outs = list(s.execute(select(OutcomeRow).where(OutcomeRow.kind == "EXTERNAL_ORDER")).scalars())
    assert len(outs) == 2 and all(o.account_id == "HACK0000" and o.severity == "info" for o in outs)


def test_strict_mode_treats_outside_orders_as_violations(monkeypatch):
    monkeypatch.setattr(settings(), "strict_monitor_021", True)
    m = Monitor(admin=EmptyPaper(), admin021=Admin021([_o("256159")]))
    m.run_once()
    assert _events("UNAUTHORIZED_BROKER_ORDER")
    with session_scope() as s:
        assert state.kill_switch(s)["engaged"]


def test_a_countersign_tagged_order_without_a_capability_engages_the_kill_switch():
    m = Monitor(admin=EmptyPaper(), admin021=Admin021([_o("256400", tag="CSDEADBEEF000001", source="api")]))
    st = m.run_once()
    assert _events("UNAUTHORIZED_BROKER_ORDER") and st["violations"] >= 1
    with session_scope() as s:
        assert state.kill_switch(s)["engaged"]


def test_unreadable_021_order_book_is_a_failed_check():
    class Down:
        def all_orders(self):
            raise RuntimeError("gateway down")

    st = Monitor(admin=EmptyPaper(), admin021=Down()).run_once()
    assert any(c["name"] == "021 order book reachable" and not c["ok"] for c in st["checks"])
