"""Paper broker accounts and key scopes (broker tables live in the test database), plus the monitor watching every
trader account. Provisioned PT accounts start exactly like DEMO1; the write key reaches every trader account but
never NAIVE1; the naive key reaches only NAIVE1; a restart keeps every provisioned account in scope."""

import secrets

import pytest
from fastapi.testclient import TestClient

from countersign.core import events, state
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.monitor.main import REPORTED_KEY, Monitor

ADMIN, NAIVE, AUDIT = "test-admin", "test-naive", "test-audit"


def _pt() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "PT" + "".join(secrets.choice(alphabet) for _ in range(6))


@pytest.fixture
def broker(monkeypatch):
    from countersign.broker import app as b

    cfg = settings()
    monkeypatch.setattr(cfg, "broker_admin_key", ADMIN)
    monkeypatch.setattr(cfg, "broker_naive_key", NAIVE)
    monkeypatch.setattr(cfg, "broker_audit_key", AUDIT)
    b.init_db()
    yield b, TestClient(b.app)  # no startup hook: the tick and housekeeping threads are not needed
    with b._sf()() as s:
        for model in (b.BTrade, b.BOrder, b.BHolding, b.BPosition):
            s.query(model).filter(model.account_id.like("PT%")).delete(synchronize_session=False)
        s.query(b.BAccount).filter(b.BAccount.id.like("PT%")).delete(synchronize_session=False)
        s.commit()


def _h(key: str, account: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {key}"}
    if account:
        h["X-Account"] = account
    return h


def _limit_buy(qty: int = 1) -> dict:
    # Far below the market so it rests OPEN (no tick is published in tests).
    return {"instrument_id": "NSE:EQ:INFY", "symbol": "INFY", "side": "BUY", "quantity": qty, "order_type": "LIMIT", "product": "CNC", "limit_price_paise": 100000, "tag": "CSTEST" + secrets.token_hex(4).upper()}


def test_provisioned_account_is_seeded_exactly_like_demo1(broker):
    b, c = broker
    pt = _pt()
    r = c.post("/admin/accounts", json={"account_id": pt}, headers=_h(ADMIN))
    assert r.status_code == 201, r.text
    holdings = {(h["instrument_id"], h["quantity"], h["avg_price_paise"]) for h in c.get("/v1/holdings", headers=_h("test-read", pt)).json()}
    assert holdings == {(iid, q, avg) for iid, _, q, avg in b.SEED_HOLDINGS["DEMO1"]}
    positions = {(x["instrument_id"], x["product"], x["quantity"], x["avg_price_paise"]) for x in c.get("/v1/positions", headers=_h("test-read", pt)).json()}
    assert positions == {(iid, prod, q, avg) for iid, _, prod, q, avg in b.SEED_POSITIONS["DEMO1"]}
    funds = c.get("/v1/funds", headers=_h("test-read", pt)).json()
    assert funds["account_id"] == pt and funds["cash_paise"] == 25_00_000_00
    assert c.post("/admin/accounts", json={"account_id": pt}, headers=_h(ADMIN)).json()["code"] == "ACCOUNT_EXISTS"
    assert c.post("/admin/accounts", json={"account_id": "NAIVE1"}, headers=_h(ADMIN)).status_code == 422
    assert c.post("/admin/accounts", json={"account_id": _pt()}, headers=_h("test-write")).status_code in (400, 403)


def test_key_scopes_cover_trader_accounts_but_never_naive(broker):
    b, c = broker
    pt = _pt()
    c.post("/admin/accounts", json={"account_id": pt}, headers=_h(ADMIN))
    for acct in ("DEMO1", "LAB1", pt):
        assert c.get("/v1/funds", headers=_h("test-read", acct)).status_code == 200
        assert c.get("/v1/funds", headers=_h("test-write", acct)).status_code == 200
    placed = c.post("/v1/orders", json=_limit_buy(), headers=_h("test-write", pt))
    assert placed.status_code == 200 and placed.json()["account_id"] == pt
    for key in ("test-read", "test-write"):
        r = c.get("/v1/orders", headers=_h(key, "NAIVE1"))
        assert r.status_code == 403 and r.json()["code"] == "ACCOUNT_FORBIDDEN"
    assert c.post("/v1/orders", json=_limit_buy(), headers=_h("test-write", "NAIVE1")).status_code == 403
    assert c.post("/v1/orders", json=_limit_buy(), headers=_h("test-read", pt)).json()["code"] == "SCOPE_FORBIDDEN"
    assert c.get("/v1/orders", headers=_h("test-write", "PTNOPE22")).status_code == 403  # not provisioned
    assert c.get("/v1/orders", headers=_h("test-write")).json()["code"] == "ACCOUNT_REQUIRED"
    assert c.get("/v1/orders", headers=_h(NAIVE, "DEMO1")).status_code == 403
    assert c.get("/v1/orders", headers=_h(NAIVE, pt)).status_code == 403
    assert c.get("/v1/orders", headers=_h(NAIVE)).status_code == 200  # NAIVE1 by default
    assert b.key_allows("@trading", "NAIVE1", "trader") is False  # NAIVE1 is refused even if mislabelled


def test_restart_keeps_provisioned_accounts_in_scope(broker):
    b, c = broker
    pt = _pt()
    c.post("/admin/accounts", json={"account_id": pt}, headers=_h(ADMIN))
    b.init_db()  # what a broker restart does: re-register key hashes, migrate, ensure base accounts
    assert c.get("/v1/funds", headers=_h("test-write", pt)).status_code == 200
    assert c.get("/v1/funds", headers=_h("test-write", "NAIVE1")).status_code == 403


def test_account_reset_is_isolated_to_one_account(broker):
    _, c = broker
    p1, p2 = _pt(), _pt()
    for p in (p1, p2):
        c.post("/admin/accounts", json={"account_id": p}, headers=_h(ADMIN))
        assert c.post("/v1/orders", json=_limit_buy(2), headers=_h("test-write", p)).json()["status"] == "OPEN"
    before_cash = c.get("/v1/funds", headers=_h("test-read", p1)).json()["available_paise"]
    r = c.post(f"/admin/accounts/{p1}/reset", headers=_h(ADMIN))
    assert r.status_code == 200 and r.json()["orders_deleted"] == 1
    assert c.get("/v1/orders", headers=_h("test-read", p1)).json() == []
    assert len(c.get("/v1/orders", headers=_h("test-read", p2)).json()) == 1
    assert c.get("/v1/funds", headers=_h("test-read", p1)).json()["available_paise"] > before_cash  # reservation released
    assert c.post("/admin/accounts/LAB1/reset", headers=_h(ADMIN)).json()["code"] == "NOT_A_TRADER_ACCOUNT"
    assert c.post("/admin/accounts/NAIVE1/reset", headers=_h(ADMIN)).status_code == 403
    assert c.post("/admin/accounts/PTZZZZZZ/reset", headers=_h(ADMIN)).status_code == 404
    assert c.post(f"/admin/accounts/{p2}/reset", headers=_h("test-write", p2)).json()["code"] == "SCOPE_FORBIDDEN"


class _Book:
    def __init__(self, *books: list[dict]):
        self.books = list(books)

    def all_orders(self) -> list[dict]:
        return self.books.pop(0) if len(self.books) > 1 else self.books[0]


def _order(oid: str, account: str) -> dict:
    return {"broker_order_id": oid, "client_order_id": None, "account_id": account, "instrument_id": "NSE:EQ:SBIN", "symbol": "SBIN", "side": "BUY", "quantity": 999, "source": "rogue"}


def test_monitor_watches_provisioned_accounts_and_skips_naive():
    pt = _pt()
    book = [_order("R-PT-1", pt), _order("R-NAIVE-1", "NAIVE1")]
    status = Monitor(admin=_Book(book)).run_once()
    auth = next(ch for ch in status["checks"] if ch["name"] == "Every broker order carries a valid countersignature")
    assert auth["ok"] is False and auth["checked"] == 1 and status["external_orders_detected"] == 1
    assert events.client().sismember(REPORTED_KEY, "ext:R-PT-1") and not events.client().sismember(REPORTED_KEY, "ext:R-NAIVE-1")
    with session_scope() as s:
        assert state.kill_switch(s)["engaged"] is True


def test_monitor_does_not_flag_an_order_deleted_by_a_reset_mid_check():
    pt = _pt()
    status = Monitor(admin=_Book([_order("R-GONE-1", pt)], [])).run_once()
    auth = next(ch for ch in status["checks"] if ch["name"] == "Every broker order carries a valid countersignature")
    assert auth["ok"] is True and status["external_orders_detected"] == 0
    with session_scope() as s:
        assert state.kill_switch(s)["engaged"] is False


def test_signup_to_portfolio_and_reset_through_the_real_broker(api, broker, monkeypatch):
    """End to end in-process: sign-up provisions a real paper account, the API reads it with the read key scoped by
    X-Account, and the per-user reset reseeds it at the broker."""
    from countersign.core import broker_client as bc
    from tests.accounts_support import csrf, signed_up

    main, _ = api
    b, broker_http = broker

    class Admin(bc.BrokerAdmin):
        def __init__(self, *a, **k):
            self._client = TestClient(b.app, headers={"Authorization": f"Bearer {ADMIN}"})

    def gateway(acct: str) -> bc.ReadGateway:
        g = bc.ReadGateway.__new__(bc.ReadGateway)
        g.account_id = acct
        g._client = TestClient(b.app, headers={"Authorization": "Bearer test-read", "X-Account": acct})
        return g

    monkeypatch.setattr(main, "BrokerAdmin", Admin)
    monkeypatch.setattr(main, "read_gateway", gateway)
    c, me = signed_up(main)
    acct = me["account"]["id"]
    summary = c.get("/api/account/summary").json()
    assert summary["account_id"] == acct and summary["holdings_count"] == 7 and summary["positions_count"] == 1
    assert summary["cash_paise"] == 25_00_000_00
    assert broker_http.post("/v1/orders", json=_limit_buy(3), headers=_h("test-write", acct)).status_code == 200
    assert len(c.get("/api/account/orders").json()) == 1
    assert c.post("/api/demo/reset", headers=csrf(c)).status_code == 200
    assert c.get("/api/account/orders").json() == []
    assert c.get("/api/account/summary").json()["holdings_count"] == 7
