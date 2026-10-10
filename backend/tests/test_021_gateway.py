"""gateway021 against a fake 021 REST server (httpx MockTransport): token handling, read retries, exact order mapping,
reconciliation of every ambiguous placement outcome, the never-resend policy, modify / cancel verification, the
paper-broker JSON shape, and the HTTP key scopes. No test calls the live sandbox."""

from __future__ import annotations

import itertools
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from countersign.broker021 import mapping, store
from countersign.broker021.client import Api021Error, Client021
from countersign.broker021.gateway import Gateway, GatewayError
from countersign.broker021.settings import GatewaySettings

ACCT = "HACK0000"
TOKENS = {"NSE:EQ:INFY": ("NSECM", 1594), "NSE:EQ:IDEA": ("NSECM", 14366), "NSE:EQ:RELIANCE": ("NSECM", 2885)}
BY_TOKEN = {v: k for k, v in TOKENS.items()}


class FakeLookup:
    def token_for(self, iid):
        return TOKENS.get(iid)

    def instrument_for(self, exchange, token):
        iid = BY_TOKEN.get((exchange, int(token)), f"021:{exchange}:{token}")
        return {"instrument_id": iid, "symbol": iid.split(":")[-1], "segment": "EQ"}

    def instrument_for_isin(self, isin, symbol=None):
        return self.instrument_for("NSECM", 1594) if symbol == "INFY" else None

    def refresh(self):
        pass


class Fake021:
    """A scripted 021 REST server. post_behaviour: ok | 500_after_accept | 503 | 500_risk | 400 | timeout_after_accept |
    timeout | drop_after_accept. Counts every request so tests can prove nothing was sent twice."""

    def __init__(self):
        self.book: list[dict] = []
        self.ids = itertools.count(300001)
        self.logins = 0
        self.posts: list[dict] = []
        self.puts: list[tuple[str, dict]] = []
        self.deletes: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.post_behaviour: list[str] = []
        self.put_behaviour: list[str] = []
        self.get_faults: list[int] = []  # statuses returned (once each) by the next GETs
        self.unauthorized_tokens: set[str] = set()
        self.always_401 = False
        self.cancel_steps: list[str] = ["Cancelled"]
        self.orders_gate: threading.Event | None = None
        self.trades: list[dict] = []
        self.positions: list[dict] = []
        self.holdings: list[dict] = []

    def add(self, o21: dict, status: str = "Pending", when: float | None = None, placed_by: str = "User") -> dict:
        row = {"orderId": next(self.ids), "time": int(when or time.time()), "lastActivity": int(when or time.time()), "token": o21["token"], "exchange": mapping.RESPONSE_EXCHANGE[o21["exchange"]], "product": o21["product"], "qtyRemaining": o21["qty"], "qtyTraded": 0, "discQty": 0, "price": o21["price"], "triggerPrice": o21.get("trigger") or 0, "book": o21["book"], "reason": "", "validity": o21["validity"], "placedBy": placed_by, "status": status, "amo": False, "exchangeOrderNumber": "1"}
        self.book.insert(0, row)
        return row

    def find(self, oid: str) -> dict | None:
        return next((o for o in self.book if str(o["orderId"]) == str(oid)), None)

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/v1", 1)[1]
        if path == "/auth/token":
            self.logins += 1
            return httpx.Response(200, json={"data": {"accessToken": f"tok{self.logins}", "tokenType": "Bearer", "expiresAt": "2099-01-01T05:00:00+05:30", "expiresIn": 3600}, "success": True, "error": None})
        tok = req.headers.get("authorization", "")[7:]
        if self.always_401 or tok in self.unauthorized_tokens:
            return httpx.Response(401, json={"data": None, "success": False, "error": "Invalid or expired access token."})
        if req.method == "GET":
            self.gets.append(path)
            if self.get_faults:
                code = self.get_faults.pop(0)
                return httpx.Response(code, json={"data": None, "success": False, "error": "Service temporarily unavailable"})
            if path == "/orders":
                if self.orders_gate is not None:
                    self.orders_gate.wait(5)
                return httpx.Response(200, json=list(self.book))
            if path.startswith("/orders/") and path.endswith("/trades"):
                return httpx.Response(200, json=[t for t in self.trades if str(t.get("orderId")) == path.split("/")[2]])
            if path.startswith("/orders/"):
                o = self.find(path.split("/")[2])
                if o is None:
                    return httpx.Response(404, json={"data": None, "success": False, "error": "Order not found."})
                if o["status"] == "SentForCancellation" and self.cancel_steps:
                    o["status"] = self.cancel_steps.pop(0)
                return httpx.Response(200, json=[dict(o)])
            if path == "/trades":
                return httpx.Response(200, json=self.trades)
            if path == "/portfolio/positions":
                return httpx.Response(200, json=self.positions)
            if path == "/portfolio/holdings":
                return httpx.Response(200, json=self.holdings)
            return httpx.Response(404, json={"error": "no route"})
        body = __import__("json").loads(req.content or b"{}")
        if req.method == "POST" and path == "/orders":
            self.posts.append(body)
            b = self.post_behaviour.pop(0) if self.post_behaviour else "ok"
            if b == "ok":
                row = self.add(body)
                return httpx.Response(200, json={"data": {"message": "Order sent to exchange", "orderId": str(row["orderId"]), "type": "SUCCESS"}, "success": True, "error": None})
            if b == "500_after_accept":
                self.add(body)
                return httpx.Response(500, json={"data": None, "success": False, "error": "Internal server error"})
            if b == "503":
                return httpx.Response(503, json={"data": None, "success": False, "error": "Service temporarily unavailable"})
            if b == "500_risk":
                return httpx.Response(500, json={"data": None, "success": False, "error": "RMS: Insufficient funds"})
            if b == "400":
                return httpx.Response(400, json={"data": None, "success": False, "error": "Invalid product"})
            if b == "timeout_after_accept":
                self.add(body)
                raise httpx.ReadTimeout("read timed out", request=req)
            if b == "timeout":
                raise httpx.ReadTimeout("read timed out", request=req)
            if b == "drop_after_accept":
                self.add(body)
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=req)
        oid = path.split("/")[2] if path.startswith("/orders/") else None
        if req.method == "PUT" and oid:
            self.puts.append((oid, body))
            b = self.put_behaviour.pop(0) if self.put_behaviour else "ok"
            if b == "timeout":
                raise httpx.ReadTimeout("read timed out", request=req)
            o = self.find(oid)
            o["price"] = body["price"]
            o["triggerPrice"] = body.get("trigger", o["triggerPrice"])
            o["qtyRemaining"] = body["qty"]
            return httpx.Response(200, json={"data": {"message": "Order modification sent to exchange succesfully", "type": "SUCCESS"}, "success": True, "error": None})
        if req.method == "DELETE" and oid:
            self.deletes.append((oid, body))
            self.find(oid)["status"] = "SentForCancellation"
            return httpx.Response(200, json={"data": {"message": "Order cancellation sent to exchange succesfully", "type": "SUCCESS"}, "success": True, "error": None})
        return httpx.Response(404, json={"error": "no route"})


def _cfg(**kw) -> GatewaySettings:
    base = {"ucc": ACCT, "password": "not-a-real-password", "reconcile_window_s": 0.3, "read_retries": 3, "clock_skew_s": 5.0}
    return GatewaySettings(**(base | kw))


@pytest.fixture
def fake():
    return Fake021()


@pytest.fixture
def gw(fake):
    store.init_db()
    _clear()
    published: list[dict] = []
    cfg = _cfg()
    client = Client021(cfg, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    g = Gateway(cfg, client=client, lookup=FakeLookup(), publish=published.append, sleep=lambda s: time.sleep(0.01))
    g.published = published
    yield g
    _clear()


def _clear():
    with store.session() as s:
        s.query(store.Attempt).filter(store.Attempt.account_id == ACCT).delete(synchronize_session=False)
        s.commit()


def _body(tag: str, **kw) -> dict:
    b = {"instrument_id": "NSE:EQ:INFY", "symbol": "INFY", "segment": "EQ", "side": "BUY", "quantity": 2, "order_type": "LIMIT", "product": "CNC", "validity": "DAY", "limit_price_paise": 99000, "tag": tag}
    return b | kw


# ------------------------------------------------------------------ token and reads


def test_login_is_single_flight_and_cached(fake):
    c = Client021(_cfg(), transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    threads = [threading.Thread(target=c.token) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.get("/orders")
    c.get("/orders")
    assert fake.logins == 1


def test_401_relogs_in_once_then_fails_closed(fake):
    c = Client021(_cfg(), transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    c.token()
    fake.unauthorized_tokens.add("tok1")  # someone else logged in: our token was revoked
    assert c.get("/orders") == []
    assert fake.logins == 2 and c.stats.relogins_on_401 == 1
    fake.always_401 = True
    with pytest.raises(Api021Error) as e:
        c.get("/orders")
    assert e.value.status == 401 and fake.logins == 3  # exactly one more login, then it fails closed


def test_reads_retry_with_backoff_on_429_500_and_503(fake):
    slept: list[float] = []
    c = Client021(_cfg(), transport=httpx.MockTransport(fake.handler), sleep=slept.append)
    fake.get_faults = [503, 429, 500]
    assert c.get("/orders") == []
    assert c.stats.read_retries == 3 and len(slept) == 3 and slept[0] < slept[2]


def test_writes_are_never_retried_by_the_client(fake):
    c = Client021(_cfg(), transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    o21 = mapping.to_021_order(_body("CSTEST021W1"), "NSECM", 1594)
    for b, kind in (("503", "unknown"), ("timeout", "unknown"), ("400", "rejected"), ("500_risk", "unknown")):
        fake.post_behaviour = [b]
        assert c.write("POST", "/orders", o21).kind == kind
    assert len(fake.posts) == 4


def test_a_recovered_failure_is_not_reported_as_current(fake):
    c = Client021(_cfg(), transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    fake.post_behaviour = ["timeout"]
    c.write("POST", "/orders", mapping.to_021_order(_body("CSTEST021R1"), "NSECM", 1594))
    assert c.stats.current_error() == "POST /orders: timeout"
    c.get("/orders")
    assert c.stats.last_error == "POST /orders: timeout" and c.stats.current_error() is None


def test_rejected_orders_reported_as_order_0_never_collide():
    o21 = mapping.to_021_order(_body("CSTEST021Z1"), "NSECM", 1594)
    for tag in ("CSTEST021Z1", "CSTEST021Z2"):
        store.create(tag, ACCT, mapping.request_fingerprint(o21), _body(tag), o21)
        store.update(tag, state="REJECTED", order_id="0", error="Price must be higher than the lower circuit limit")
        assert store.get(tag)["state"] == "REJECTED" and store.get(tag)["broker_order_id"] is None
    assert store.by_order_id("0") is None and "0" not in store.mapped_order_ids(ACCT)  # a rejected row is never matched to us by id
    store.delete_tags("CSTEST021Z")


# ------------------------------------------------------------------ mapping


def test_signed_orders_map_exactly_to_021():
    m = mapping.to_021_order
    assert m(_body("t", product="MIS"), "NSECM", 1594) == {"exchange": "NSE", "token": 1594, "qty": 2, "price": 99000, "book": "RL", "trigger": 0, "discQuantity": 0, "product": "INTRADAY", "validity": "Day", "amo": False}
    sell = m(_body("t", side="SELL", order_type="MARKET", limit_price_paise=None, protection_price_paise=98500, validity="IOC"), "NSECM", 1594)
    assert sell["qty"] == -2 and sell["book"] == "RL" and sell["price"] == 98500 and sell["validity"] == "IOC"
    sl = m(_body("t", order_type="SL", limit_price_paise=101000, trigger_price_paise=100500), "NSECM", 1594)
    assert sl["book"] == "SL" and sl["price"] == 101000 and sl["trigger"] == 100500
    slm = m(_body("t", order_type="SL-M", limit_price_paise=None, trigger_price_paise=100500, protection_price_paise=101000), "NSECM", 1594)
    assert slm["book"] == "SL" and slm["price"] == 101000 and slm["trigger"] == 100500
    fo = m(_body("t", product="NRML", quantity=65), "NSEFO", 35008)
    assert fo["exchange"] == "NSEFO" and fo["product"] == "NRML"
    with pytest.raises(mapping.MappingError):
        m(_body("t"), "NSEIDX", 26000)
    with pytest.raises(mapping.MappingError):
        m(_body("t", order_type="SL", trigger_price_paise=None), "NSECM", 1594)


def test_status_mapping_keeps_the_021_status():
    row = {"orderId": 1, "status": "Pending", "qtyRemaining": -3, "qtyTraded": 0, "price": 99000, "book": "RL", "product": "INTRADAY", "validity": "Day", "exchange": "NSECM", "token": 1594, "time": 1791525084}
    o = mapping.order_json(row, account_id=ACCT, instrument={"instrument_id": "NSE:EQ:INFY", "symbol": "INFY", "segment": "EQ"})
    assert o["status"] == "OPEN" and o["status_021"] == "Pending" and o["side"] == "SELL" and o["product"] == "MIS" and o["validity"] == "DAY"
    assert mapping.order_status(row | {"qtyTraded": 1, "qtyRemaining": -2}) == "PARTIALLY_FILLED"
    assert mapping.order_status(row | {"book": "SL"}) == "TRIGGER_PENDING"
    for st, ours in (("Executed", "COMPLETE"), ("Rejected", "REJECTED"), ("Cancelled", "CANCELLED"), ("SentForCancellation", "OPEN"), ("AcceptedForAMO", "OPEN")):
        assert mapping.order_status(row | {"status": st}) == ours
    from countersign.broker.app import _order_json, BOrder

    paper_keys = set(_order_json(BOrder(id="1", account_id="A", tag=None, instrument_id="i", symbol="s", segment="EQ", side="BUY", quantity=1, filled_quantity=0, order_type="LIMIT", product="CNC", validity="DAY", status="OPEN", source="api", placed_at="", updated_at="")).keys())
    assert paper_keys <= set(o.keys())


def test_trade_times_are_corrected_to_unix_seconds():
    assert mapping.unix_ts(2106901823) == 2106901823 - 315513000
    assert mapping.unix_ts(1791525084) == 1791525084


# ------------------------------------------------------------------ placement and reconciliation


def test_accepted_placement_is_tagged_and_found_by_tag(gw, fake):
    o = gw.place(_body("CSTEST021A1"))
    assert o["client_order_id"] == "CSTEST021A1" and o["status"] == "OPEN" and o["status_021"] == "Pending" and o["price_021_paise"] == 99000
    assert fake.posts[0]["qty"] == 2 and fake.posts[0]["product"] == "CNC"
    rows = gw.orders(tag="CSTEST021A1")
    assert len(rows) == 1 and rows[0]["broker_order_id"] == o["broker_order_id"]
    assert gw.orders(tag="CSTEST021NOPE") == []


@pytest.mark.parametrize("behaviour", ["500_after_accept", "timeout_after_accept", "drop_after_accept"])
def test_lost_answer_after_acceptance_is_reconciled_and_never_resent(gw, fake, behaviour):
    fake.post_behaviour = [behaviour]
    o = gw.place(_body("CSTEST021R" + behaviour[:6].upper()))
    assert o["reconciled"] is True and o["client_order_id"].startswith("CSTEST021R")
    assert len(fake.posts) == 1 and len(fake.book) == 1
    assert store.get(o["client_order_id"])["state"] == "ACCEPTED"


@pytest.mark.parametrize("behaviour", ["503", "timeout"])
def test_unanswered_placement_absent_from_the_book_is_not_placed(gw, fake, behaviour):
    fake.post_behaviour = [behaviour]
    with pytest.raises(GatewayError) as e:
        gw.place(_body("CSTEST021N" + behaviour[:3].upper()))
    assert e.value.status == 502 and e.value.code == "NOT_PLACED" and e.value.extra["reconciled"] is True
    assert len(fake.posts) == 1 and fake.book == [] and "/orders" in fake.gets


def test_two_matching_orders_are_ambiguous_never_guessed(gw, fake):
    o21 = mapping.to_021_order(_body("x"), "NSECM", 1594)
    fake.add(o21, placed_by="User")  # an identical order placed in the 021 web app at the same moment
    fake.post_behaviour = ["500_after_accept"]
    with pytest.raises(GatewayError) as e:
        gw.place(_body("CSTEST021AMB"))
    assert e.value.code == "AMBIGUOUS" and len(e.value.extra["candidates"]) == 2
    assert len(fake.posts) == 1 and store.get("CSTEST021AMB")["broker_order_id"] is None


def test_orders_placed_before_the_send_are_not_candidates(gw, fake):
    o21 = mapping.to_021_order(_body("x"), "NSECM", 1594)
    fake.add(o21, when=time.time() - 120)  # an identical order from two minutes ago
    fake.post_behaviour = ["500_after_accept"]
    o = gw.place(_body("CSTEST021OLD"))
    assert o["reconciled"] is True and len(fake.posts) == 1
    assert o["broker_order_id"] == str(fake.book[0]["orderId"])  # the new one, not the old one


def test_risk_rejection_in_a_500_is_reconciled_then_reported_as_rejected(gw, fake):
    fake.post_behaviour = ["500_risk"]
    with pytest.raises(GatewayError) as e:
        gw.place(_body("CSTEST021RSK"))
    assert e.value.status == 422 and e.value.code == "REJECTED_BY_021" and "Insufficient funds" in e.value.message
    assert len(fake.posts) == 1 and "/orders" in fake.gets


def test_definite_rejection_is_reported_without_resend(gw, fake):
    fake.post_behaviour = ["400"]
    with pytest.raises(GatewayError) as e:
        gw.place(_body("CSTEST021400"))
    assert e.value.code == "REJECTED_BY_021" and len(fake.posts) == 1


def test_a_tag_is_never_sent_twice(gw, fake):
    first = gw.place(_body("CSTEST021TAG"))
    again = gw.place(_body("CSTEST021TAG"))
    assert again["broker_order_id"] == first["broker_order_id"] and again["replayed_tag"] is True
    assert len(fake.posts) == 1
    fake.post_behaviour = ["503"]
    with pytest.raises(GatewayError):
        gw.place(_body("CSTEST021TG2", quantity=3))
    with pytest.raises(GatewayError) as e:
        gw.place(_body("CSTEST021TG2", quantity=3))
    assert e.value.code == "TAG_ALREADY_USED" and len(fake.posts) == 2


def test_identical_order_is_refused_while_one_is_unresolved(gw, fake):
    fake.post_behaviour = ["503"]
    fake.orders_gate = threading.Event()  # hold the first placement inside its reconciliation
    errors: list[GatewayError] = []

    def first():
        try:
            gw.place(_body("CSTEST021DUP1"))
        except GatewayError as e:
            errors.append(e)

    t = threading.Thread(target=first)
    t.start()
    end = time.time() + 3
    while (store.get("CSTEST021DUP1") or {}).get("state") != "SENDING" or not fake.posts:
        assert time.time() < end
        time.sleep(0.02)
    with pytest.raises(GatewayError) as e:
        gw.place(_body("CSTEST021DUP2"))
    assert e.value.status == 409 and e.value.code == "DUPLICATE_IN_FLIGHT"
    fake.orders_gate.set()
    t.join(5)
    assert errors and errors[0].code == "NOT_PLACED"
    with pytest.raises(GatewayError) as e2:  # still held for a short while after NOT_PLACED: it may appear late
        gw.place(_body("CSTEST021DUP3"))
    assert e2.value.code == "DUPLICATE_IN_FLIGHT"
    gw.fingerprint_hold_s = 0.0
    assert gw.place(_body("CSTEST021DUP4"))["status"] == "OPEN"
    assert len(fake.posts) == 2  # DUP2 and DUP3 were never sent


def test_late_acceptance_after_not_placed_is_mapped_and_published(gw, fake):
    fake.post_behaviour = ["503"]
    with pytest.raises(GatewayError):
        gw.place(_body("CSTEST021LATE"))
    fake.add(mapping.to_021_order(_body("x"), "NSECM", 1594))  # 021 shows it after all
    gw.sync()
    att = store.get("CSTEST021LATE")
    assert att["state"] == "ACCEPTED" and att["note"].startswith("Late")
    assert any(o["client_order_id"] == "CSTEST021LATE" for o in gw.published)
    assert len(fake.posts) == 1


def test_crashed_gateway_reconciles_sending_attempts_on_startup(gw, fake):
    o21 = mapping.to_021_order(_body("x"), "NSECM", 1594)
    store.create("CSTEST021CRSH", ACCT, mapping.request_fingerprint(o21), _body("CSTEST021CRSH"), o21)
    fake.add(o21)  # it had reached 021 before the crash
    out = gw.recover()
    assert out and out[0]["reconciled"] is True and store.get("CSTEST021CRSH")["state"] == "ACCEPTED"
    assert fake.posts == []


# ------------------------------------------------------------------ modify / cancel


def test_modify_sends_the_full_new_state_and_is_verified(gw, fake):
    o = gw.place(_body("CSTEST021MOD", side="SELL", product="MIS"))
    out = gw.modify(o["broker_order_id"], None, 99500, None)
    oid, body = fake.puts[0]
    assert oid == o["broker_order_id"] and body == {"exchange": "NSE", "token": 1594, "qty": -2, "price": 99500, "book": "RL", "product": "INTRADAY", "validity": "Day"}
    assert out["modification"] == "APPLIED" and out["limit_price_paise"] == 99500 and out["side"] == "SELL"


def test_modify_whose_answer_is_lost_and_not_applied_is_unconfirmed_not_resent(gw, fake):
    o = gw.place(_body("CSTEST021MTO"))
    fake.put_behaviour = ["timeout"]
    with pytest.raises(GatewayError) as e:
        gw.modify(o["broker_order_id"], None, 99500, None)
    assert e.value.code == "MODIFY_UNCONFIRMED" and len(fake.puts) == 1


def test_cancel_is_confirmed_on_the_order_itself(gw, fake):
    o = gw.place(_body("CSTEST021CXL"))
    fake.cancel_steps = ["SentForCancellation", "Cancelled"]
    out = gw.cancel(o["broker_order_id"])
    assert out["status"] == "CANCELLED" and out["cancellation"] == "CONFIRMED"
    assert fake.deletes == [(o["broker_order_id"], {"exchange": "NSE", "token": 1594, "product": "CNC"})]
    with pytest.raises(GatewayError) as e:
        gw.cancel(o["broker_order_id"])
    assert e.value.code == "ORDER_NOT_OPEN" and len(fake.deletes) == 1


# ------------------------------------------------------------------ account reads


def test_funds_are_an_estimate_and_portfolio_maps_to_paper_shape(gw, fake):
    fake.add(mapping.to_021_order(_body("x", quantity=10, limit_price_paise=100000), "NSECM", 1594))
    fake.trades = [{"tradeId": 1, "tradeTime": 2106901823, "quantity": 10, "token": 1594, "exchange": "NSECM", "product": "INTRADAY", "price": 99000, "symbol": "INFY"}, {"tradeId": 2, "tradeTime": 2106901830, "quantity": -4, "token": 1594, "exchange": "NSECM", "product": "INTRADAY", "price": 99500, "symbol": "INFY"}]
    fake.positions = [{"exchange": "NSECM", "token": 1594, "product": "INTRADAY", "netQuantity": 6, "netPrice": 99000, "carryForwardQuantity": 0, "longPosition": True, "squareOffQuantity": -6, "buyPrice": 99000, "sellPrice": 99500, "prevClose": 99200}]
    fake.holdings = [{"isin": "INE009A01021", "symbol": "INFY", "freeQty": 5, "btstQty": 2, "sellQty": 0, "price": 98000, "prevClose": 99200}]
    f = gw.funds()
    assert f["estimate"] is True and f["used_margin_paise"] == 10 * 100000
    assert f["cash_paise"] == 10_00_000_00 - (10 * 99000 - 4 * 99500) and f["available_paise"] == f["cash_paise"] - f["used_margin_paise"]
    p = gw.positions()[0]
    assert p["instrument_id"] == "NSE:EQ:INFY" and p["product"] == "MIS" and p["quantity"] == 6 and p["realised_paise"] == 4 * (99500 - 99000)
    h = gw.holdings()[0]
    assert h["instrument_id"] == "NSE:EQ:INFY" and h["quantity"] == 7 and h["avg_price_paise"] == 98000
    # Today's delivery buys are holdings (sellable today), never also positions.
    gw.invalidate()
    fake.positions.append({"exchange": "NSECM", "token": 1594, "product": "CNC", "netQuantity": 3, "netPrice": 101000, "carryForwardQuantity": 0, "longPosition": True, "squareOffQuantity": -3, "buyPrice": 101000, "sellPrice": 0, "prevClose": 99200})
    h = gw.holdings()[0]
    assert h["quantity"] == 10 and h["today_quantity"] == 3 and h["avg_price_paise"] == (98000 * 7 + 101000 * 3) // 10
    assert [p["product"] for p in gw.positions()] == ["MIS"]
    t = gw.trades()
    assert t[0]["ts"].startswith("2026-")


# ------------------------------------------------------------------ HTTP scopes


def test_http_scopes_match_the_paper_broker_and_refuse_the_naive_key(gw, fake, monkeypatch):
    from countersign.broker021 import app as gapp
    from countersign.broker021.settings import gateway_settings
    from countersign.core.config import settings

    monkeypatch.setattr(gateway_settings(), "test_faults", False)  # independent of the developer's .env

    cfg = settings()
    for name, val in (("broker_read_key", "k-read"), ("broker_write_key", "k-write"), ("broker_admin_key", "k-admin"), ("broker_audit_key", "k-audit"), ("broker_naive_key", "k-naive")):
        monkeypatch.setattr(cfg, name, val)
    monkeypatch.setattr(gapp, "_gw", gw)
    c = TestClient(gapp.app)

    def h(k, acct=ACCT):
        out = {"Authorization": f"Bearer {k}"}
        if acct:
            out["X-Account"] = acct
        return out

    body = _body("CSTEST021HTTP")
    assert c.post("/v1/orders", json=body, headers=h("k-read")).status_code == 403
    assert c.post("/v1/orders", json=body, headers=h("k-naive")).status_code == 401
    assert c.post("/v1/orders", json=body, headers=h("k-write", "PTABCDEF")).status_code == 403
    assert c.get("/v1/orders", headers=h("k-read", None)).json()["code"] == "ACCOUNT_REQUIRED"
    r = c.post("/v1/orders", json=body, headers=h("k-write"))
    assert r.status_code == 200 and r.json()["client_order_id"] == "CSTEST021HTTP"
    assert c.get("/v1/orders", params={"tag": "CSTEST021HTTP"}, headers=h("k-read")).json()[0]["broker_order_id"] == r.json()["broker_order_id"]
    assert c.get("/v1/attempts/CSTEST021HTTP", headers=h("k-read")).json()["state"] == "ACCEPTED"
    assert c.get("/v1/attempts/CSTEST021NONE", headers=h("k-read")).json()["code"] == "TAG_UNKNOWN"
    assert c.get("/admin/orders", headers=h("k-audit", None)).status_code == 200
    assert c.get("/admin/orders", headers=h("k-read")).status_code == 403
    assert c.post("/admin/faults", json={"mode": "drop_after_accept"}, headers=h("k-admin", None)).json()["code"] == "FAULTS_DISABLED"
    fake.post_behaviour = ["503"]
    r = c.post("/v1/orders", json=_body("CSTEST021HTP2", quantity=9), headers=h("k-write"))
    assert r.status_code == 502 and r.json()["code"] == "NOT_PLACED"
    assert c.get("/v1/account", headers=h("k-read", None)).json() == {"account_id": ACCT, "broker": "021", "label": "HACK***0"}


def test_fault_drills_not_placed_sends_nothing_and_drop_after_accept_reconciles(gw, fake, monkeypatch):
    from countersign.broker021 import app as gapp
    from countersign.broker021.settings import gateway_settings
    from countersign.core.config import settings

    cfg = settings()
    for name, val in (("broker_read_key", "k-read"), ("broker_write_key", "k-write"), ("broker_admin_key", "k-admin"), ("broker_audit_key", "k-audit")):
        monkeypatch.setattr(cfg, name, val)
    monkeypatch.setattr(gapp, "_gw", gw)
    monkeypatch.setattr(gateway_settings(), "test_faults", True)
    monkeypatch.setattr(gapp, "_faults", {"drop_after_accept": 0, "fail_before_send": 0})
    c = TestClient(gapp.app)
    admin = {"Authorization": "Bearer k-admin"}
    write = {"Authorization": "Bearer k-write", "X-Account": ACCT}
    assert c.post("/admin/faults", json={"mode": "unknown"}, headers=admin).status_code == 422
    assert c.post("/admin/faults", json={"mode": "fail_before_send"}, headers=admin).json() == {"enabled": True, "drop_after_accept": 0, "fail_before_send": 1}
    r = c.post("/v1/orders", json=_body("CSTEST021NOTP"), headers=write)
    assert r.status_code == 502 and r.json()["code"] == "NOT_PLACED" and "fault drill" in r.json()["message"]
    assert fake.posts == [] and fake.book == []  # not one byte reached 021
    assert c.get("/v1/attempts/CSTEST021NOTP", headers={"Authorization": "Bearer k-read", "X-Account": ACCT}).json()["state"] == "NOT_PLACED"
    # The fault was used once; countersigning again (a new tag) sends exactly one order.
    r = c.post("/v1/orders", json=_body("CSTEST021NOT2"), headers=write)
    assert r.status_code == 200 and len(fake.posts) == 1 and len(fake.book) == 1
    assert c.post("/admin/faults", json={"mode": "drop_after_accept"}, headers=admin).json()["drop_after_accept"] == 1
    r = c.post("/v1/orders", json=_body("CSTEST021DROP", quantity=3), headers=write)
    assert r.status_code == 200 and r.json().get("reconciled") is True and len(fake.posts) == 2 and len(fake.book) == 2
    assert c.post("/admin/faults", json={"mode": "none"}, headers=admin).json() == {"enabled": True, "drop_after_accept": 0, "fail_before_send": 0}


def test_market_order_filled_in_many_small_trades_reports_the_weighted_average(gw, fake):
    """Live sandbox behaviour: a market order fills in several small trades, and some rest Pending part filled. The
    filled quantity is 021's traded quantity and the average is weighted over the order's own trades."""
    o21 = {"exchange": "NSE", "token": 1594, "qty": 10, "price": 99500, "book": "RL", "trigger": 0, "product": "CNC", "validity": "Day"}
    row = fake.add(o21, status="Executed")
    row.update(qtyRemaining=0, qtyTraded=10)
    oid = row["orderId"]
    fake.trades = [
        {"tradeId": 11, "orderId": oid, "tradeTime": 2107038142, "quantity": 3, "token": 1594, "exchange": "NSECM", "product": "CNC", "price": 99000},
        {"tradeId": 12, "orderId": oid, "tradeTime": 2107038143, "quantity": 2, "token": 1594, "exchange": "NSECM", "product": "CNC", "price": 99100},
        {"tradeId": 13, "orderId": oid, "tradeTime": 2107038145, "quantity": 5, "token": 1594, "exchange": "NSECM", "product": "CNC", "price": 98950},
        {"tradeId": 14, "orderId": 999999, "tradeTime": 2107038146, "quantity": 7, "token": 1594, "exchange": "NSECM", "product": "CNC", "price": 1},
    ]
    o = gw.to_order(dict(row))
    assert o["status"] == "COMPLETE" and o["filled_quantity"] == 10 and o["side"] == "BUY"
    assert o["avg_fill_price_paise"] == (3 * 99000 + 2 * 99100 + 5 * 98950) // 10
    part = fake.add(o21 | {"qty": -8}, status="Pending")
    part.update(qtyRemaining=-5, qtyTraded=-3)
    fake.trades += [
        {"tradeId": 21, "orderId": part["orderId"], "tradeTime": 2107038150, "quantity": -1, "token": 1594, "exchange": "NSECM", "product": "CNC", "price": 99400},
        {"tradeId": 22, "orderId": part["orderId"], "tradeTime": 2107038151, "quantity": -2, "token": 1594, "exchange": "NSECM", "product": "CNC", "price": 99300},
    ]
    po = gw.to_order(dict(part))
    assert po["status"] == "PARTIALLY_FILLED" and po["filled_quantity"] == 3 and po["pending_quantity"] == 5 and po["side"] == "SELL"
    assert po["avg_fill_price_paise"] == (99400 + 2 * 99300) // 3
    assert mapping.unix_ts(2107038142) == 1791525142  # trade times arrive shifted; corrected to Unix seconds
