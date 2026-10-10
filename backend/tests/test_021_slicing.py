"""On a 021 account an order above one share is signed once and sent as one-share orders, each after the previous
one fills: 021's sandbox fills only about half of a larger order and holds the rest open."""

import pytest

from countersign.core import proposals
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import Account
from countersign.core.parser import parse
from countersign.core.verifier import AccountState
from tests.test_l2_l4_support import Gw, holding, ltp, user_id, view

ACCT = "HACKSLICE"


@pytest.fixture
def acct021(monkeypatch):
    monkeypatch.setattr(settings(), "gateway021_enabled", True)
    with session_scope() as s:
        if s.get(Account, ACCT) is None:
            s.add(Account(id=ACCT, user_id=user_id(), label="021 slicing test", segments=["EQ", "FO"], trading_enabled=True, broker_kind="021"))
    return ACCT


def _draft(text: str, account_id: str, gw: Gw | None = None) -> dict:
    p = parse(text)
    acct = AccountState.load(gw or Gw(), account_id)
    with session_scope() as s:
        if p.plan is not None:
            out = proposals.create_plan(s, p.plan, user_text=text, account=acct, user_id=user_id(), engine="deterministic", origin="test")
        else:
            out = proposals.create_order(s, p.order, user_text=text, account=acct, user_id=user_id(), engine="deterministic", origin="test", facts=p.facts)
        assert out.proposal is not None, out.clarify
        pid = out.proposal.id
    return view(pid)


def _steps(v: dict) -> list[tuple[str, str, int, int | None]]:
    return [(st["canonical"]["side"], st["canonical"]["instrument"]["symbol"], st["canonical"]["quantity"], st["depends_on"]) for st in v["steps"]]


def test_a_two_share_order_on_021_is_signed_once_as_two_chained_one_share_orders(acct021):
    ltp("RELIANCE")
    v = _draft("Buy 2 RELIANCE at market", acct021)
    assert v["kind"] == "PLAN" and v["status"] != "BLOCKED"
    assert _steps(v) == [("BUY", "RELIANCE", 1, None), ("BUY", "RELIANCE", 1, 1)]
    assert "SLICE_021" in {i["code"] for i in v["issues"]}
    q = {f["key"]: f for f in v["steps"][1]["fields"]}["quantity"]
    assert q["display"] == "1 (share 2 of 2)"
    assert v["summary"].endswith("sent to 021 as 2 one-share orders")


def test_sell_then_buy_runs_every_share_in_order_and_the_buy_waits_for_the_last_sell(acct021):
    r, _ = ltp("RELIANCE")
    ltp("TCS")
    v = _draft("Sell 5 RELIANCE at market and then buy 3 TCS at market", acct021, Gw(holdings=[holding("RELIANCE", 9, r)]))
    assert v["status"] != "BLOCKED", v["issues"]
    steps = _steps(v)
    assert len(steps) == 8 and sum(q for _, _, q, _ in steps) == 8
    assert steps[:5] == [("SELL", "RELIANCE", 1, None)] + [("SELL", "RELIANCE", 1, n) for n in range(1, 5)]
    assert steps[5:] == [("BUY", "TCS", 1, 5), ("BUY", "TCS", 1, 6), ("BUY", "TCS", 1, 7)]


def test_selling_more_than_held_is_still_blocked_after_slicing(acct021):
    r, _ = ltp("RELIANCE")
    v = _draft("Sell 5 RELIANCE at market", acct021, Gw(holdings=[holding("RELIANCE", 3, r)]))
    assert v["status"] == "BLOCKED"


def test_paper_accounts_and_large_or_stop_orders_are_sent_whole(acct021):
    ltp("RELIANCE")
    from tests.test_l2_l4_support import ACCOUNT

    assert _draft("Buy 2 RELIANCE at market", ACCOUNT)["kind"] == "ORDER"
    assert _draft("Buy 25 RELIANCE at market", acct021)["kind"] == "ORDER"
    assert _draft("Buy 1 RELIANCE at market", acct021)["kind"] == "ORDER"


def test_slicing_can_be_switched_off(acct021, monkeypatch):
    ltp("RELIANCE")
    monkeypatch.setattr(settings(), "slice_021", False)
    assert _draft("Buy 2 RELIANCE at market", acct021)["kind"] == "ORDER"


def test_a_plan_missing_prices_offers_one_click_at_market_for_every_step():
    p = parse("sell 5 of reliance and then buy 3 stocks of tcs")
    assert p.kind == "clarify" and p.options == ["sell 5 of reliance at market and then buy 3 stocks of tcs at market"]
    assert parse(p.options[0]).kind == "plan"
    assert parse("Sell 5 RELIANCE at 1200 and then buy 3 TCS").options == ["Sell 5 RELIANCE at 1200 and then buy 3 TCS at market"]
