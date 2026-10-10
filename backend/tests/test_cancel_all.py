"""'Cancel all my open orders': one plan of independent CANCEL steps, one per order the broker still shows as open,
partially filled or trigger pending (whatever day it was placed), approved with one countersignature."""

import pytest
from sqlalchemy import select

from countersign.core import proposals
from countersign.core.db import session_scope
from countersign.core.models import Plan, PlanStep
from countersign.core.parser import detect_cancel_all, parse
from countersign.core.verifier import AccountState, build_cancel_all_plan
from countersign.llm import chat, router
from countersign.llm import tools as T
from tests.test_l2_l4_support import ACCOUNT, Gw, account, ltp, open_order, proposal_count, user_id, view


def _o(oid, symbol, side, qty, otype, *, status, filled=0, placed_at="2026-10-10T09:20:00", **kw):
    o = open_order(oid, symbol, side, qty, otype, status=status, **kw)
    o["filled_quantity"] = filled
    o["pending_quantity"] = qty - filled if status in ("OPEN", "PARTIALLY_FILLED", "TRIGGER_PENDING") else 0
    o["placed_at"] = placed_at
    return o


def _book() -> Gw:
    r, _ = ltp("RELIANCE")
    i, _ = ltp("INFY")
    t, _ = ltp("TCS")
    ltp("SBIN")
    return Gw(
        orders=[
            _o("256508", "RELIANCE", "BUY", 10, "LIMIT", limit=r - 500, status="PARTIALLY_FILLED", filled=5, placed_at="2026-10-09T14:02:00"),
            _o("256600", "INFY", "SELL", 4, "SL", limit=i - 1000, trigger=i - 900, status="TRIGGER_PENDING"),
            _o("256700", "TCS", "BUY", 2, "LIMIT", limit=t - 1000, status="OPEN"),
            _o("256800", "SBIN", "BUY", 3, "LIMIT", limit=100, status="COMPLETE", filled=3),
            _o("256801", "SBIN", "BUY", 3, "LIMIT", limit=100, status="REJECTED"),
            _o("256802", "INFY", "BUY", 3, "LIMIT", limit=100, status="CANCELLED"),
        ]
    )


def _cancel_all(text: str, gw: Gw) -> proposals.Outcome:
    with session_scope() as s:
        out = proposals.create_cancel_all_plan(s, user_text=text, account=account(gw), user_id=user_id(), engine="deterministic", origin="test")
        if out.proposal is not None:
            s.expunge(out.proposal)
        return out


# ---------------------------------------------------------------- parser


@pytest.mark.parametrize(
    "text",
    ["Cancel all my open orders", "cancel all orders", "Cancel all pending orders", "cancel every open order", "Cancel all my outstanding orders", "cancel my open orders"],
)
def test_cancel_all_phrases_parse_to_one_cancel_all_intent(text):
    p = parse(text)
    assert p.kind == "cancel_all" and p.args == {"symbol": None}, (p.kind, p.question)


def test_cancel_all_for_one_symbol_and_single_cancels_still_work():
    assert parse("Cancel all my RELIANCE orders").args == {"symbol": "RELIANCE"}
    assert parse("cancel all orders in INFY").args == {"symbol": "INFY"}
    single = parse("cancel order 256508")
    assert single.kind == "cancel" and single.order is not None and single.order.target_order_ref == "256508"
    assert detect_cancel_all("cancel order 256508") is None
    assert detect_cancel_all("what are my open orders?") is None
    assert parse("what are my open orders?").kind == "query"
    assert detect_cancel_all("Cancel all my orders and buy 10 INFY") is None


# ---------------------------------------------------------------- the plan


def test_cancel_all_is_one_plan_of_independent_cancels_of_open_orders_only():
    out = _cancel_all("Cancel all my open orders", _book())
    v = view(out.proposal.id)
    assert v["kind"] == "PLAN" and v["status"] == "AWAITING_APPROVAL", v["issues"]
    steps = [st["canonical"] for st in v["steps"]]
    assert [(c["action"], c["target_broker_order_id"], c["instrument"]["symbol"]) for c in steps] == [
        ("CANCEL", "256508", "RELIANCE"),  # placed yesterday, still partly open at the broker
        ("CANCEL", "256600", "INFY"),
        ("CANCEL", "256700", "TCS"),
    ]
    assert all(st["depends_on"] is None for st in v["steps"])
    assert v["steps"][0]["summary"] == "CANCEL order 256508 (BUY 10 RELIANCE, 5 of 10 filled)"
    assert v["steps"][2]["summary"] == "CANCEL order 256700 (BUY 2 TCS)"
    assert v["summary"].startswith("Cancel 3 open orders: (1) CANCEL order 256508")
    step1 = {i["code"]: i for i in v["steps"][0]["issues"]}
    assert "keeps what already filled" in step1["PARTIAL_FILL_KEPT"]["message"]
    assert "keeps what already filled" in step1["CANCEL_ALL_SCOPE"]["message"] and "earlier days" in step1["CANCEL_ALL_SCOPE"]["message"]
    assert "CANCEL_ALL_SCOPE" in {i["code"] for i in v["issues"]}
    with session_scope() as s:
        assert s.get(Plan, out.proposal.id).state == "AWAITING_APPROVAL"
        rows = s.execute(select(PlanStep).where(PlanStep.plan_id == out.proposal.id).order_by(PlanStep.step_no)).scalars().all()
        assert [r.depends_on for r in rows] == [None, None, None]
        assert [r.canonical["action"] for r in rows] == ["CANCEL"] * 3


def test_cancel_all_for_one_symbol_only_cancels_that_symbol():
    gw = _book()
    gw._o.append(_o("256900", "RELIANCE", "SELL", 1, "LIMIT", limit=10_000_00, status="OPEN"))
    v = view(_cancel_all("Cancel all my RELIANCE orders", gw).proposal.id)
    assert [st["canonical"]["target_broker_order_id"] for st in v["steps"]] == ["256508", "256900"]
    assert v["summary"].startswith("Cancel 2 open RELIANCE orders:")


def test_builder_ignores_terminal_orders_even_if_the_gateway_returns_them():
    ltp("RELIANCE")
    acct = AccountState(account_id=ACCOUNT, open_orders=[_o("1", "RELIANCE", "BUY", 3, "LIMIT", limit=100, status="COMPLETE", filled=3), _o("2", "RELIANCE", "BUY", 3, "LIMIT", limit=100, status="OPEN")])
    with session_scope() as s:
        d = build_cancel_all_plan(s, acct)
    assert [o.target_broker_order_id for o in d.orders] == ["2"] and d.message is None


def test_nothing_to_cancel_drafts_nothing():
    before = proposal_count()
    gw = Gw(orders=[_o("256800", "SBIN", "BUY", 3, "LIMIT", limit=100, status="COMPLETE", filled=3)])
    out = _cancel_all("Cancel all my open orders", gw)
    assert out.proposal is None and out.notice is not None
    assert "nothing to cancel" in out.notice[1]
    out = _cancel_all("Cancel all my TCS orders", _book().__class__(orders=[]))
    assert "no open TCS orders" in out.notice[1]
    assert proposal_count() == before


# ---------------------------------------------------------------- routing


def _say(text: str, gw: Gw) -> tuple[str, list[dict]]:
    cards: list[dict] = []
    d = T.ChatDeps(user_id=user_id(), account_id=ACCOUNT, conversation_id="c-ca", user_text=text, engine="deterministic", emit=cards.append, origin="test", gateway=gw)
    return router.handle(d), cards


def test_deterministic_engine_drafts_the_cancel_all_plan_and_says_when_there_is_nothing():
    text, cards = _say("Cancel all my open orders", _book())
    ids = [c["proposal_id"] for c in cards if c["kind"] == "proposal"]
    assert len(ids) == 1 and "keeps what already filled" in text
    assert [st["canonical"]["action"] for st in view(ids[0])["steps"]] == ["CANCEL"] * 3
    before = proposal_count()
    text, cards = _say("Cancel all my open orders", Gw())
    assert "nothing to cancel" in text and cards[-1]["kind"] == "notice"
    assert proposal_count() == before


def test_cancel_all_takes_the_deterministic_fast_path():
    assert "cancel_all" in chat.FULLY_PARSED
    assert chat._instruments_known(parse("Cancel all my open orders"))
    assert chat._instruments_known(parse("Cancel all my RELIANCE orders"))
    assert not chat._instruments_known(parse("Cancel all my FOOBARXYZ orders"))
