"""Test fixtures: an isolated Postgres database (countersign_test), Valkey db 1, and a fault-injecting fake broker
plugged into the REAL executor pipeline (pipeline._brokers), so tests exercise production code paths."""

import os

os.environ["CS_DATABASE_URL"] = "postgresql+psycopg://countersign:countersign_local@127.0.0.1:55432/countersign_test"
# The paper broker's tables (b_*) live in the same test database, never in the running stack's paperbroker DB.
os.environ["CS_BROKER_DATABASE_URL"] = "postgresql+psycopg://countersign:countersign_local@127.0.0.1:55432/countersign_test"
os.environ["CS_VALKEY_URL"] = "redis://127.0.0.1:56379/1"
os.environ["CS_BROKER_READ_KEY"] = "test-read"
os.environ["CS_BROKER_WRITE_KEY"] = "test-write"
os.environ["CS_MARKET_ALWAYS_OPEN"] = "true"
os.environ["CS_RECONCILE_WINDOW_S"] = "0.6"

import itertools
import json
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from countersign.core import events
from countersign.core.broker_client import BrokerTimeout, BrokerUnavailable
from countersign.core.db import session_scope

TRANSACTIONAL = [
    "outbox", "exec_jobs", "capabilities", "approvals", "plan_steps", "plans", "mandates", "proposals", "outcomes",
    "broker_orders", "order_fills", "chat_messages", "security_events", "attack_runs", "market_snapshots", "system_state",
]


@pytest.fixture(scope="session", autouse=True)
def _bootstrap():
    from countersign.core.bootstrap import bootstrap

    bootstrap()
    yield


@pytest.fixture(autouse=True)
def published(monkeypatch):
    """Pub/sub channels are shared by every Valkey database, so a test must never really publish: the running
    stack's feed, executor tracker and live UI would receive it. Messages are captured here instead."""
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr(events, "publish", lambda channel, payload: out.append((channel, payload)))
    return out


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.execute(text("TRUNCATE " + ", ".join(TRANSACTIONAL) + " CASCADE"))
    events.client().flushdb()
    yield


def set_tick(instrument_id: str, ltp_paise: int, prev: int | None = None, seq: int = 1, ts: datetime | None = None) -> None:
    t = {
        "instrument_id": instrument_id,
        "symbol": instrument_id.split(":")[-1],
        "ltp_paise": ltp_paise,
        "bid_paise": ltp_paise - 5,
        "ask_paise": ltp_paise + 5,
        "volume": 1000,
        "prev_close_paise": prev or ltp_paise,
        "ts": (ts or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "seq": seq,
    }
    events.client().set(events.TICK_KEY.format(instrument_id), json.dumps(t))


class FakeBroker:
    """Stands in for the external broker. Faults: 'accept_then_timeout', 'unavailable'. Never dedupes tags."""

    def __init__(self):
        self.book: list[dict] = []
        self.faults: list[str] = []
        self.lock = threading.Lock()
        self.seq = itertools.count(1)

    def _new(self, body: dict) -> dict:
        o = {
            "broker_order_id": f"B{next(self.seq):06d}", "client_order_id": body.get("tag"), "account_id": "LAB1",
            "instrument_id": body["instrument_id"], "symbol": body["symbol"], "side": body["side"], "quantity": body["quantity"],
            "filled_quantity": 0, "pending_quantity": body["quantity"], "avg_fill_price_paise": None, "order_type": body["order_type"],
            "product": body["product"], "validity": body.get("validity", "DAY"), "limit_price_paise": body.get("limit_price_paise"),
            "trigger_price_paise": None, "protection_price_paise": body.get("protection_price_paise"), "status": "OPEN",
            "reject_reason": None, "placed_at": "", "updated_at": "",
        }
        self.book.append(o)
        return o

    def place(self, body: dict) -> dict:
        with self.lock:
            fault = self.faults.pop(0) if self.faults else None
            if fault == "unavailable":
                raise BrokerUnavailable("connection refused")
            o = self._new(body)
            if fault == "accept_then_timeout":
                raise BrokerTimeout("response lost after acceptance")
            return dict(o)

    def orders_by(self, tag=None):
        return [dict(o) for o in self.book if tag is None or o["client_order_id"] == tag]

    def orders(self, status="all", tag=None):  # ReadGateway-compatible
        return self.orders_by(tag)

    def modify(self, oid, body):
        o = next(o for o in self.book if o["broker_order_id"] == oid)
        o["limit_price_paise"] = body.get("limit_price_paise")
        return dict(o)

    def cancel(self, oid):
        o = next(o for o in self.book if o["broker_order_id"] == oid)
        o["status"] = "CANCELLED"
        return dict(o)


@pytest.fixture
def fake_broker():
    from countersign.executor import pipeline

    fb = FakeBroker()
    pipeline._brokers.clear()
    pipeline._brokers["LAB1"] = fb
    pipeline._brokers["DEMO1"] = fb
    yield fb
    pipeline._brokers.clear()


class FakeGateway:
    """ReadGateway stand-in for building AccountState in tests (LAB1 holdings + funds)."""

    def funds(self):
        return {"cash_paise": 25_00_000_00, "available_paise": 25_00_000_00, "used_margin_paise": 0}

    def holdings(self):
        return [{"instrument_id": "NSE:EQ:INFY", "symbol": "INFY", "quantity": 100, "avg_price_paise": 182040}]

    def positions(self):
        return []

    def orders(self, status="all", tag=None):
        return []


@pytest.fixture
def account():
    from countersign.core.verifier import AccountState

    return AccountState.load(FakeGateway(), "LAB1")


@pytest.fixture
def api(monkeypatch):
    """The API app with the paper broker's admin API stubbed; test users are removed afterwards."""
    from tests.accounts_support import api_fixture

    yield from api_fixture(monkeypatch)
