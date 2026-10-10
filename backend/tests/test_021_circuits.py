"""021 runtime circuit limits learned from refusals (core/circuits021.py): parsing both refusal texts, never widening,
expiry at the next 08:00 IST, the startup backfill from stored REJECTED attempts, learning from a refused placement,
and the verifier blocking a 021 order outside a learned limit with a plain message. Paper accounts are unaffected.
No test calls the live sandbox."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from countersign.broker021 import store
from countersign.broker021.client import Client021
from countersign.broker021.gateway import Gateway, backfill_circuits
from countersign.core import circuits021, events
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import Account
from countersign.core.money import inr
from countersign.core.schemas import CanonicalOrder
from countersign.core.verifier import AccountState, verify_order
from tests.test_021_gateway import Fake021, FakeLookup, _body, _cfg
from tests.test_l2_l4_support import Gw, draft_order, inst, ltp, rupees, user_id

LIVE = "Price 2,090.10 must be higher than the lower circuit limit 2,104.00"
ACCT021 = "HACKCIRC"
TAGP = "CSCIRC"
IST = 19800


def _ist(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() - IST


# ------------------------------------------------------------------ parsing


def test_parses_the_exact_live_refusal():
    assert circuits021.parse_refusal(LIVE) == {"lower": 210400}


@pytest.mark.parametrize(
    "msg,expect",
    [
        ("Price 2,300.00 must be lower than the upper circuit limit 2,288.30", {"upper": 228830}),
        ("PRICE 2090.10 SHOULD BE GREATER THAN LOWER CIRCUIT LIMIT: 2104", {"lower": 210400}),
        ("price must be above the lower-circuit limit of Rs. 2,104.00.", {"lower": 210400}),
        ("Price 1,00,500.50 must be less than the Upper  Circuit  Limit 1,00,250.25", {"upper": 10025025}),
        ("Price 99.00 must be higher than the lower circuit limit 99.005", {"lower": 9901}),  # sub-paise: never wider
        ("price 120.00 has to be below the upper circuit limit is 119.999", {"upper": 11999}),
    ],
)
def test_parses_both_refusals_tolerantly(msg, expect):
    assert circuits021.parse_refusal(msg) == expect


@pytest.mark.parametrize("msg", [None, "", "RMS: Insufficient funds", "Invalid product", "Price must be a multiple of tick size 0.05"])
def test_other_refusals_teach_nothing(msg):
    assert circuits021.parse_refusal(msg) == {}


# ------------------------------------------------------------------ storage: never widening, expiry


def test_merge_never_widens():
    cur, ch = circuits021.merge(None, lower=210400, upper=None, at=1.0, source="a")
    assert ch and cur["lower_paise"] == 210400 and "upper_paise" not in cur
    same, ch = circuits021.merge(cur, lower=200000, upper=None, at=2.0, source="b")
    assert not ch and same["lower_paise"] == 210400 and same["lower_learned_at"] == 1.0
    up, ch = circuits021.merge(same, lower=211000, upper=228830, at=3.0, source="c")
    assert ch and up["lower_paise"] == 211000 and up["upper_paise"] == 228830
    keep, ch = circuits021.merge(up, lower=None, upper=230000, at=4.0, source="d")
    assert not ch and keep["upper_paise"] == 228830
    down, ch = circuits021.merge(keep, lower=None, upper=220000, at=5.0, source="e")
    assert ch and down["upper_paise"] == 220000 and down["lower_paise"] == 211000


def test_learn_never_widens_and_holds_for_five_minutes():
    iid = "NSE:EQ:TCS"
    assert circuits021.learn_from_refusal(iid, LIVE, source="t")["lower_paise"] == 210400
    circuits021.learn_from_refusal(iid, "Price 2,000.00 must be higher than the lower circuit limit 2,050.00", source="t")
    circuits021.learn_from_refusal(iid, "Price 2,400.00 must be lower than the upper circuit limit 2,200.00", source="t")
    circuits021.learn_from_refusal(iid, "Price 2,400.00 must be lower than the upper circuit limit 2,300.00", source="t")
    got = circuits021.learned(iid)
    assert got["lower_paise"] == 210400 and got["upper_paise"] == 220000
    ttl = events.client().ttl(circuits021.KEY.format(iid))
    assert abs(ttl - min(circuits021.HOLD_S, circuits021.next_reset() - time.time())) < 5
    assert circuits021.learned(iid, now=time.time() + circuits021.HOLD_S + 1) is None  # a passing refusal stops blocking


def test_trading_day_boundaries():
    # 16:37 IST on 9 Oct: the day began at 08:00 IST that morning and its limits expire at 08:00 IST on 10 Oct.
    now = _ist(2026, 10, 9, 16, 37)
    assert circuits021.day_start(now) == _ist(2026, 10, 9, 8, 0)
    assert circuits021.next_reset(now) == _ist(2026, 10, 10, 8, 0)
    # 07:30 IST on 10 Oct still belongs to the 9 Oct trading day.
    early = _ist(2026, 10, 10, 7, 30)
    assert circuits021.day_start(early) == _ist(2026, 10, 9, 8, 0)
    assert circuits021.next_reset(early) == _ist(2026, 10, 10, 8, 0)
    assert circuits021.ist_hhmm(_ist(2026, 10, 9, 16, 37)) == "16:37"


def test_a_refusal_from_an_earlier_trading_day_is_not_learned_or_used():
    iid = "NSE:EQ:TCS"
    yesterday = circuits021.day_start() - 3600
    assert circuits021.learn_from_refusal(iid, LIVE, at=yesterday, source="t") is None
    assert circuits021.learned(iid) is None
    # A stale value somehow still stored (no expiry) is ignored too.
    events.client().set(circuits021.KEY.format(iid), json.dumps({"lower_paise": 210400, "lower_learned_at": yesterday}))
    assert circuits021.learned(iid) is None


# ------------------------------------------------------------------ gateway: backfill and live refusals


@pytest.fixture
def attempts():
    store.init_db()
    store.delete_tags(TAGP)
    yield
    store.delete_tags(TAGP)


def _attempt(tag: str, iid: str, state: str, error: str | None, sent_at: float | None = None) -> None:
    store.create(tag, "HACKBF00", "fp-" + tag, {"instrument_id": iid, "tag": tag}, {})
    store.update(tag, state=state, error=error)
    if sent_at is not None:
        store.update(tag, sent_at=sent_at)


def test_backfill_learns_todays_stored_refusals_only(attempts):
    _attempt(TAGP + "1", "NSE:EQ:TCS", "REJECTED", LIVE)
    _attempt(TAGP + "2", "NSE:EQ:INFY", "REJECTED", "Price 1,700.00 must be lower than the upper circuit limit 1,650.00", sent_at=circuits021.day_start() - 600)
    _attempt(TAGP + "3", "NSE:EQ:ITC", "REJECTED", "RMS: Insufficient funds")
    _attempt(TAGP + "4", "NSE:EQ:RELIANCE", "ACCEPTED", "Price 1.00 must be higher than the lower circuit limit 2.00")
    out = backfill_circuits()
    assert out["NSE:EQ:TCS"]["lower_paise"] == 210400
    assert circuits21_none(["NSE:EQ:INFY", "NSE:EQ:ITC", "NSE:EQ:RELIANCE"])
    got = circuits021.learned("NSE:EQ:TCS")
    assert got["lower_paise"] == 210400 and TAGP + "1" in got["lower_source"]
    assert abs(got["lower_learned_at"] - store.get(TAGP + "1")["sent_at"]) < 1


def circuits21_none(iids: list[str]) -> bool:
    return all(circuits021.learned(i) is None for i in iids)


class CircuitFake(Fake021):
    """021 refuses the next placement with a circuit message (HTTP 422) and shows it Rejected in its order book."""

    msg = "Price 990.00 must be higher than the lower circuit limit 1,004.00"

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/orders") and self.post_behaviour[:1] == ["circuit"]:
            self.post_behaviour.pop(0)
            body = json.loads(req.content or b"{}")
            self.posts.append(body)
            row = self.add(body)
            row["status"], row["reason"] = "Rejected", self.msg
            return httpx.Response(422, json={"data": None, "success": False, "error": self.msg})
        return super().handler(req)


def test_a_refused_placement_teaches_the_limit(attempts):
    fake = CircuitFake()
    cfg = _cfg(ucc="HACKCRF0")
    client = Client021(cfg, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    g = Gateway(cfg, client=client, lookup=FakeLookup(), publish=lambda o: None, sleep=lambda s: None)
    fake.post_behaviour = ["circuit"]
    try:
        o = g.place(_body(TAGP + "LIVE"))  # like the live TCS case: refused (422) and found Rejected in the order book
        assert o["status"] == "REJECTED" and o["reconciled"] is True and len(fake.posts) == 1
    finally:
        with store.session() as s:
            s.query(store.Attempt).filter(store.Attempt.account_id == "HACKCRF0").delete(synchronize_session=False)
            s.commit()
    got = circuits021.learned("NSE:EQ:INFY")
    assert got is not None and got["lower_paise"] == 100400


def test_a_rejected_row_in_the_order_book_teaches_the_limit(attempts):
    fake = Fake021()
    cfg = _cfg(ucc="HACKCRF1")
    client = Client021(cfg, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    g = Gateway(cfg, client=client, lookup=FakeLookup(), publish=lambda o: None, sleep=lambda s: None)
    row = fake.add({"exchange": "NSE", "token": 1594, "qty": 1, "price": 99000, "book": "RL", "trigger": 0, "product": "CNC", "validity": "DAY"})
    row["status"], row["reason"] = "Rejected", "Price 990.00 must be lower than the upper circuit limit 980.00"
    g.sync(publish_all=True)
    assert circuits021.learned("NSE:EQ:INFY")["upper_paise"] == 98000


# ------------------------------------------------------------------ verifier


@pytest.fixture
def acct021(monkeypatch):
    monkeypatch.setattr(settings(), "gateway021_enabled", True)
    with session_scope() as s:
        if s.get(Account, ACCT021) is None:
            s.add(Account(id=ACCT021, user_id=user_id(), label="021 circuit test", segments=["EQ", "FO"], trading_enabled=True, broker_kind="021"))
    return ACCT021


def _issues(order: CanonicalOrder, account_id: str):
    acct = AccountState.load(Gw(), account_id)
    with session_scope() as s:
        issues, _ = verify_order(s, order, acct, skip_session=True)
    return issues


def _order(text_: str) -> CanonicalOrder:
    out = draft_order(text_, Gw())
    assert out.proposal is not None, out.clarify
    return CanonicalOrder.model_validate(out.proposal.canonical)


def test_021_market_and_limit_orders_below_a_learned_lower_limit_are_blocked_plainly(acct021):
    p, t = ltp("TCS")
    i = inst("TCS")
    assert i.lower_circuit_paise < p < i.upper_circuit_paise
    market = _order("Buy 1 TCS at market")
    assert market.protection_price_paise is not None
    lower = market.protection_price_paise + 40 * t  # like the live case: 021's runtime lower limit is above the live price
    assert lower < i.upper_circuit_paise
    at = time.time() - 60
    circuits021.learn("NSE:EQ:TCS", lower=lower, at=at, source="test")
    want = f"021 will not accept TCS below {inr(lower)} today (it refused an order below that price at {circuits021.ist_hhmm(at)} IST)."
    blocks = [x for x in _issues(market, acct021) if x.code == "CIRCUIT_021"]
    assert len(blocks) == 1 and blocks[0].severity == "BLOCK" and blocks[0].field == "protection_price"
    assert blocks[0].message.startswith(want) and f"Use a limit of at least {inr(lower)} or choose another stock." in blocks[0].message

    limit = _order(f"Buy 1 TCS at {rupees(p)}")
    blocks = [x for x in _issues(limit, acct021) if x.code == "CIRCUIT_021"]
    assert len(blocks) == 1 and blocks[0].field == "limit_price" and blocks[0].message.startswith(want)

    ok = _order(f"Buy 1 TCS at {rupees(lower)}")
    assert not [x for x in _issues(ok, acct021) if x.code in ("CIRCUIT_021", "PRICE_BAND")]

    # The paper account is unaffected by what 021 refused.
    for o in (market, limit):
        assert not [x for x in _issues(o, "LAB1") if x.code in ("CIRCUIT_021", "PRICE_BAND")]


def test_021_sell_above_a_learned_upper_limit_is_blocked(acct021):
    p, t = ltp("TCS")
    upper = p - 40 * t
    circuits021.learn("NSE:EQ:TCS", upper=upper, source="test")
    o = _order(f"Sell 1 TCS at {rupees(p)} intraday")
    blocks = [x for x in _issues(o, acct021) if x.code == "CIRCUIT_021"]
    assert len(blocks) == 1 and f"021 will not accept TCS above {inr(upper)} today" in blocks[0].message
    assert not [x for x in _issues(o, "LAB1") if x.code == "CIRCUIT_021"]


def test_021_without_a_learned_limit_keeps_the_instruments_file_band(acct021):
    p, _ = ltp("TCS")
    o = _order("Buy 1 TCS at market")
    assert not [x for x in _issues(o, acct021) if x.code in ("CIRCUIT_021", "PRICE_BAND")]
    # A learned limit looser than the file never widens it.
    i = inst("TCS")
    circuits021.learn("NSE:EQ:TCS", lower=i.lower_circuit_paise - 100, upper=i.upper_circuit_paise + 100, source="test")
    below = _order(f"Buy 1 TCS at {rupees(i.lower_circuit_paise - i.tick_size_paise)}")
    codes = [x.code for x in _issues(below, acct021)]
    assert "PRICE_BAND" in codes and "CIRCUIT_021" not in codes
