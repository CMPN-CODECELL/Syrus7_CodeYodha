"""While a demo scenario (presenter Spike or Crash) drives a 021-priced instrument, those simulated prices never reach
the real 021 account: its orders are blocked by the verifier and its standing instructions do not fire. Paper accounts
keep using the scenario, which is what the demos need."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from countersign.core import events, market
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.mandate_engine import evaluate
from countersign.core.market import Tick
from countersign.core.models import Account, StandingMandate
from countersign.core.parser import parse
from countersign.core.schemas import CanonicalOrder
from countersign.core.verifier import AccountState, verify_order
from tests.test_l2_l4_support import Gw, draft_order, inst, ltp, rupees, user_id
from tests.test_mandate_already_true import _draft

ACCT = "HACKSIM0"


@pytest.fixture
def live021(monkeypatch):
    monkeypatch.setattr(settings(), "gateway021_enabled", True)
    with session_scope() as s:
        if s.get(Account, ACCT) is None:
            s.add(Account(id=ACCT, user_id=user_id(), label="021 scenario test", segments=["EQ", "FO"], trading_enabled=True, broker_kind="021"))
    iid = inst("RELIANCE").instrument_id
    yield iid
    events.client().delete(market.SCENARIO_KEY.format(iid))
    with session_scope() as s:
        s.execute(text("DELETE FROM mandates WHERE account_id=:a"), {"a": ACCT})


def _codes(order: CanonicalOrder, account_id: str) -> set[str]:
    acct = AccountState.load(Gw(), account_id)
    with session_scope() as s:
        issues, _ = verify_order(s, order, acct, skip_session=True)
    return {i.code for i in issues}


def test_021_order_is_blocked_while_a_scenario_drives_the_price(live021):
    p, _ = ltp("RELIANCE")
    out = draft_order(f"Buy 1 RELIANCE at {rupees(p)}", Gw())
    order = CanonicalOrder.model_validate(out.proposal.canonical)
    assert "SIMULATED_PRICE" not in _codes(order, ACCT)
    events.client().set(market.SCENARIO_KEY.format(live021), "1", ex=60)
    assert "SIMULATED_PRICE" in _codes(order, ACCT)
    # A paper account keeps trading on the scenario price: that is the demo.
    assert "SIMULATED_PRICE" not in _codes(order, "LAB1")


def test_scenario_flag_is_ignored_without_the_gateway(monkeypatch, live021):
    events.client().set(market.SCENARIO_KEY.format(live021), "1", ex=60)
    monkeypatch.setattr(settings(), "gateway021_enabled", False)
    assert market.scenario_driven(live021) is False


def test_021_standing_alert_does_not_fire_on_a_simulated_price(live021):
    p, t = ltp("RELIANCE")
    x = p - 20 * t
    out = _draft(f"Alert me if RELIANCE falls below {rupees(x)}")
    assert out.proposal is not None, out.clarify
    with session_scope() as s:
        s.execute(text("UPDATE mandates SET state='ACTIVE', account_id=:a, last_evaluated_at=NULL WHERE proposal_id=:p"), {"a": ACCT, "p": out.proposal.id})
    from datetime import UTC, datetime

    tick = Tick(instrument_id=live021, symbol="RELIANCE", ltp_paise=x - t, bid_paise=x - t, ask_paise=x - t, volume=1, prev_close_paise=p, ts=datetime.now(UTC), seq=99)
    events.client().set(market.SCENARIO_KEY.format(live021), "1", ex=60)
    with session_scope() as s:
        m = s.execute(text("SELECT id FROM mandates WHERE proposal_id=:p"), {"p": out.proposal.id}).scalar_one()
        assert evaluate(s, s.get(StandingMandate, m), tick) == "NONE"
    events.client().delete(market.SCENARIO_KEY.format(live021))
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, m), tick) == "TRIGGERED"


def test_parse_of_alert_used_above_is_a_price_alert():
    assert parse("Alert me if RELIANCE falls below 1000").mandate.kind == "ALERT"


def test_replay_only_instrument_is_blocked_on_021_before_the_countersign(live021):
    # TATAMOTORS has no 021 token (it demerged into TMPV and TMCV): its price here is simulated, so the 021 account
    # must never be asked to countersign it. A paper account still trades it on the replay feed.
    p, _ = ltp("TATAMOTORS")
    out = draft_order(f"Buy 1 TATAMOTORS at {rupees(p)}", Gw())
    order = CanonicalOrder.model_validate(out.proposal.canonical)
    assert "INSTRUMENT_NOT_ON_021" in _codes(order, ACCT)
    assert "INSTRUMENT_NOT_ON_021" not in _codes(order, "LAB1")
