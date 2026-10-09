"""A standing instruction whose condition is already true when drafted is never armed silently: the reply states the
live price and offers complete sentences (an order now, or a trigger on the far side of the price) that parse back."""

from sqlalchemy import func, select, text

from countersign.core import proposals
from countersign.core.db import session_scope
from countersign.core.models import Proposal, StandingMandate
from countersign.core.parser import parse
from tests.test_l2_l4_support import Gw, account, inst, ltp, rupees, user_id, view


def _draft(text: str) -> proposals.Outcome:
    with session_scope() as s:
        return proposals.create_mandate(s, parse(text).mandate, user_text=text, account=account(Gw()), user_id=user_id(), engine="deterministic", origin="test")


def _counts() -> tuple[int, int]:
    with session_scope() as s:
        return s.execute(select(func.count()).select_from(Proposal)).scalar_one(), s.execute(select(func.count()).select_from(StandingMandate)).scalar_one()


def test_official_example_far_above_the_price_asks_instead_of_arming():
    p, t = ltp("RELIANCE")
    before = _counts()
    out = _draft(f"If Reliance drops below {rupees(p * 2)}, buy 10")
    assert out.proposal is None and out.clarify is not None
    q, options = out.clarify
    assert "right now" in q and "already true" in q and "I have not armed it" in q
    assert _counts() == before
    now_opt, trig_opt = options
    assert now_opt.startswith("Buy 10 RELIANCE now at limit ")
    assert trig_opt.startswith("If RELIANCE drops below ") and trig_opt.endswith(", buy 10")
    # Twice the price is outside the band, so the order now uses the live price as its limit and says so.
    assert "outside today's price band" in q
    o = parse(now_opt)
    assert o.kind == "order" and o.order.side.value == "BUY" and o.order.quantity == 10
    assert int(o.order.limit_price * 100) == p
    m = parse(trig_opt)
    assert m.kind == "mandate" and m.mandate.op == "LTE" and int(m.mandate.threshold_price * 100) < p


def test_trigger_option_drafts_an_instruction_that_waits():
    p, _ = ltp("RELIANCE")
    out = _draft(f"Buy 100 shares of Reliance when it falls below {rupees(p + p // 10)}")
    trig = out.clarify[1][1]
    nxt = _draft(trig)
    assert nxt.proposal is not None
    v = view(nxt.proposal.id)
    assert v["status"] == "AWAITING_APPROVAL", v["issues"]
    assert nxt.proposal.canonical["predicate"]["threshold"] < p
    assert nxt.proposal.canonical["template"]["quantity"] == 100


def test_order_now_keeps_the_traders_limit_when_it_is_in_band():
    p, t = ltp("RELIANCE")
    limit = p + 5 * t
    out = _draft(f"If Reliance drops below {rupees(limit)}, buy 10")
    now_opt = out.clarify[1][0]
    assert int(parse(now_opt).order.limit_price * 100) == limit
    assert "outside today's price band" not in out.clarify[0]


def test_rise_condition_already_true_offers_a_higher_trigger():
    p, t = ltp("INFY")
    out = _draft(f"If INFY rises above {rupees(p - 10 * t)}, sell 5")
    q, (now_opt, trig_opt) = out.clarify
    assert now_opt.startswith("Sell 5 INFY now at limit ")
    m = parse(trig_opt)
    assert m.mandate.op == "GTE" and int(m.mandate.threshold_price * 100) > p and m.mandate.order.side.value == "SELL"


def test_price_alert_already_true_asks_with_a_level_beyond_the_price():
    p, _ = ltp("TCS")
    out = _draft(f"Alert me if TCS falls below {rupees(p + 100_00)}")
    assert out.proposal is None
    q, (opt,) = out.clarify
    assert "already true" in q and "I have not set it" in q
    m = parse(opt)
    assert m.mandate.kind == "ALERT" and int(m.mandate.threshold_price * 100) < p


def test_condition_not_yet_true_is_drafted_as_before():
    p, t = ltp("RELIANCE")
    out = _draft(f"If Reliance drops below {rupees(p - 30 * t)}, buy 10")
    assert out.clarify is None and out.proposal is not None
    assert view(out.proposal.id)["status"] == "AWAITING_APPROVAL"


def test_instrument_without_a_live_tick_is_not_questioned():
    from countersign.core import events

    i = inst("ITC")
    events.client().delete(events.TICK_KEY.format(i.instrument_id))
    # With no live price the verifier cannot tell, so it does not invent a question; approval binds a live price later.
    out = _draft("If ITC drops below 100000, buy 10")
    assert out.clarify is None or "already true" not in out.clarify[0]


def test_order_now_rounds_an_off_tick_live_price_onto_the_tick_grid():
    from tests.conftest import set_tick

    i = inst("RELIANCE")
    t = i.tick_size_paise
    assert t > 1
    off = i.prev_close_paise - i.prev_close_paise % t + 3  # the 021 sandbox publishes prices off the tick grid
    set_tick(i.instrument_id, off, prev=off)
    q, (now_opt, _) = _draft(f"If Reliance drops below {rupees(off * 3)}, buy 10").clarify
    limit = int(parse(now_opt).order.limit_price * 100)
    assert limit % t == 0 and off <= limit < off + t



def test_identical_active_alert_blocks_a_second_copy():
    text_ = "Tell me when any holding falls 3% in a day"
    first = _draft(text_)
    assert first.proposal is not None
    with session_scope() as s:
        m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == first.proposal.id)).scalar_one()
        m.state = "ACTIVE"
    try:
        second = _draft(text_)
        codes = {i["code"]: i["severity"] for i in second.proposal.issues}
        assert codes.get("DUPLICATE_MANDATE") == "BLOCK" and second.proposal.status == "BLOCKED"
    finally:
        with session_scope() as s:
            s.execute(text("UPDATE mandates SET state='CANCELLED' WHERE user_id=:u AND state IN ('ACTIVE','PENDING_APPROVAL')"), {"u": user_id()})
