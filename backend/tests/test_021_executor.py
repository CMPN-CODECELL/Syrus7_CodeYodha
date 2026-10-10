"""Executor policy on the real pipeline: 021 accounts are never retried automatically (NOT_PLACED / NEEDS_REVIEW are
recorded and surfaced), and three fixes that apply to every broker: a 5xx is reconciled instead of being taken as a
rejection, MODIFY / CANCEL are confirmed on the target order, and crash recovery never resends an expired or
previous-day order."""

from __future__ import annotations


import pytest
from sqlalchemy import select, text

from countersign.core import state
from countersign.core.broker_client import BrokerError, BrokerTimeout, BrokerUnavailable, _kinds
from countersign.core.db import session_scope
from countersign.core.models import Account, Approval, ExecJob, OutboxEntry, OutcomeRow, Proposal
from countersign.executor import pipeline
from tests.conftest import FakeBroker, FakeGateway, set_tick
from tests.helpers import approve, job_for, lab_user_id, propose, run_job

ACCT = "HACK0000"


# ------------------------------------------------------------------ fakes


class Gateway021Fake:
    """What the executor sees of gateway021 (the /v1 interface). behaviour: ok | not_placed | ambiguous | duplicate |
    rejected | unavailable | timeout_then_accepted | timeout_then_unknown | http500_then_not_placed."""

    def __init__(self, behaviour: str = "ok"):
        self.behaviour = behaviour
        self.places: list[dict] = []
        self.book: dict[str, dict] = {}
        self.attempts: dict[str, dict] = {}

    def _order(self, body: dict, oid: str) -> dict:
        return {"broker_order_id": oid, "client_order_id": body.get("tag"), "account_id": ACCT, "instrument_id": body["instrument_id"], "symbol": body["symbol"], "side": body["side"], "quantity": body["quantity"], "filled_quantity": 0, "pending_quantity": body["quantity"], "avg_fill_price_paise": None, "order_type": body["order_type"], "product": body["product"], "validity": body.get("validity", "DAY"), "limit_price_paise": body.get("limit_price_paise"), "trigger_price_paise": None, "protection_price_paise": body.get("protection_price_paise"), "status": "OPEN", "reject_reason": None, "broker": "021", "status_021": "Pending", "price_021_paise": body.get("limit_price_paise")}

    def place(self, body: dict) -> dict:
        self.places.append(body)
        b = self.behaviour
        if b == "ok":
            o = self._order(body, "256300")
            self.book[o["broker_order_id"]] = o
            return o
        if b == "not_placed":
            raise BrokerError(502, "NOT_PLACED", "021 answered: HTTP 503. The order is not in 021's order book after 8 s of checking, so it was not placed. Nothing was resent.", {"code": "NOT_PLACED", "broker_error": "HTTP 503"})
        if b == "ambiguous":
            raise BrokerError(502, "AMBIGUOUS", "2 orders in the 021 order book match this one.", {"code": "AMBIGUOUS", "candidates": ["256301", "256302"]})
        if b == "duplicate":
            raise BrokerError(409, "DUPLICATE_IN_FLIGHT", "An identical order is still being confirmed with 021, so this one was not sent.")
        if b == "rejected":
            raise BrokerError(422, "REJECTED_BY_021", "RMS: Insufficient funds")
        if b == "unavailable":
            raise BrokerUnavailable("connection refused")
        if b == "timeout_then_accepted":
            o = self._order(body, "256303")
            self.book[o["broker_order_id"]] = o
            self.attempts[body["tag"]] = {"tag": body["tag"], "state": "ACCEPTED", "broker_order_id": "256303"}
            raise BrokerTimeout("read timed out")
        if b == "timeout_then_unknown":
            raise BrokerTimeout("read timed out")
        if b == "http500_then_not_placed":
            self.attempts[body["tag"]] = {"tag": body["tag"], "state": "NOT_PLACED", "error": "HTTP 503"}
            raise BrokerError(500, "INTERNAL", "gateway error")
        raise AssertionError(b)

    def attempt(self, tag: str, wait_s: float = 0.0) -> dict:
        if tag not in self.attempts:
            raise BrokerError(404, "TAG_UNKNOWN", "never received")
        return self.attempts[tag]

    def order(self, oid: str) -> dict:
        return dict(self.book[oid])

    def orders(self, status="all", tag=None):
        return [dict(o) for o in self.book.values() if tag is None or o["client_order_id"] == tag]


class PaperFake(FakeBroker):
    """The conftest fake broker plus order() and two more faults: accept_then_503 and modify_lost."""

    def place(self, body: dict) -> dict:
        if self.faults and self.faults[0] == "accept_then_503":
            self.faults.pop(0)
            self._new(body)
            raise BrokerError(503, "UNAVAILABLE", "Service temporarily unavailable")
        return super().place(body)

    def order(self, oid: str) -> dict:
        return dict(next(o for o in self.book if o["broker_order_id"] == oid))

    def modify(self, oid, body):
        self.modify_calls = getattr(self, "modify_calls", 0) + 1
        if self.faults and self.faults[0] == "modify_lost_applied":
            self.faults.pop(0)
            super().modify(oid, body)
            raise BrokerTimeout("response lost")
        if self.faults and self.faults[0] == "modify_lost_not_applied":
            self.faults.pop(0)
            raise BrokerTimeout("response lost")
        return super().modify(oid, body)


class BookGateway(FakeGateway):
    """AccountState source that also lists the fake broker's open orders (so MODIFY / CANCEL can target them)."""

    def __init__(self, fb):
        self.fb = fb

    def orders(self, status="all", tag=None):
        return [dict(o) for o in self.fb.book if status != "open" or o["status"] in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED")]


LAB_EMAIL = "lab021@executor.test.invalid"


@pytest.fixture
def acct021(monkeypatch):
    """A 021-kind account owned by the Attack Lab test user, who is listed in CS_021_OWNER_EMAILS for the test
    (the executor routes by broker_kind and requires the approver to be a listed owner)."""
    from countersign.core.config import settings
    from countersign.core.models import User

    with session_scope() as s:
        if s.get(Account, ACCT) is None:
            s.add(Account(id=ACCT, user_id=lab_user_id(), label="021 test", segments=["EQ", "FO"], trading_enabled=True, broker_kind="021"))
        s.get(User, lab_user_id()).email = LAB_EMAIL
    monkeypatch.setattr(settings(), "owner_emails_021", LAB_EMAIL)
    monkeypatch.setattr(settings(), "slice_021", False)  # these cover how one order is sent; slicing has its own tests
    from countersign.broker021 import linking

    monkeypatch.setattr(linking, "gateway_account_id", lambda refresh=False: ACCT)  # ACCT is the gateway's .env account
    _kinds.pop(ACCT, None)
    from countersign.core.verifier import AccountState

    yield AccountState.load(FakeGateway(), ACCT)
    pipeline._brokers.pop(ACCT, None)
    with session_scope() as s:
        s.get(User, lab_user_id()).email = None


def _outbox(pid: str) -> OutboxEntry:
    with session_scope() as s:
        return s.execute(select(OutboxEntry).where(OutboxEntry.proposal_id == pid)).scalar_one()


def _outcomes(pid: str) -> list[str]:
    with session_scope() as s:
        return [o.kind for o in s.execute(select(OutcomeRow).where(OutcomeRow.proposal_id == pid)).scalars()]


def _approved_021(acct021, behaviour: str) -> tuple[str, Gateway021Fake]:
    fake = Gateway021Fake(behaviour)
    pipeline._brokers[ACCT] = fake
    set_tick("NSE:EQ:INFY", 186000)
    pid = propose("buy 5 INFY at 1860", acct021)
    approve(pid, hold=True)
    return pid, fake


# ------------------------------------------------------------------ 021: never retried


def test_021_accepted_order_is_acked(acct021):
    pid, fake = _approved_021(acct021, "ok")
    out = run_job(job_for(pid).id)
    assert out["ok"] and out["send"]["state"] == "ACKED" and len(fake.places) == 1
    assert fake.places[0]["tag"].startswith("CS") and fake.places[0]["limit_price_paise"] == 186000


@pytest.mark.parametrize("behaviour", ["not_placed", "duplicate", "unavailable", "timeout_then_unknown", "http500_then_not_placed"])
def test_021_not_placed_is_recorded_once_and_never_retried(acct021, behaviour):
    pid, fake = _approved_021(acct021, behaviour)
    out = run_job(job_for(pid).id)
    assert out["send"]["state"] == "NOT_PLACED" and len(fake.places) == 1
    ob = _outbox(pid)
    assert ob.state == "NOT_PLACED" and ob.attempts == 1 and "countersign again to resend" in ob.reject_reason
    with session_scope() as s:
        assert s.get(Proposal, pid).status == "FAILED"
        assert not state.kill_switch(s)["engaged"]
    assert "ORDER_NOT_PLACED" in _outcomes(pid)


def test_021_ambiguous_needs_review_without_kill_switch(acct021):
    pid, fake = _approved_021(acct021, "ambiguous")
    out = run_job(job_for(pid).id)
    assert out["send"]["state"] == "NEEDS_REVIEW" and len(fake.places) == 1
    assert _outbox(pid).state == "NEEDS_REVIEW" and "ORDER_NEEDS_REVIEW" in _outcomes(pid)
    with session_scope() as s:
        assert not state.kill_switch(s)["engaged"]


def test_021_lost_gateway_answer_is_resolved_by_tag_not_resent(acct021):
    pid, fake = _approved_021(acct021, "timeout_then_accepted")
    out = run_job(job_for(pid).id)
    assert out["ok"] and out["send"]["state"] == "RECONCILED_FOUND" and out["send"]["broker_order_id"] == "256303"
    assert len(fake.places) == 1


def test_021_definite_rejection_is_reported(acct021):
    pid, fake = _approved_021(acct021, "rejected")
    out = run_job(job_for(pid).id)
    assert out["send"]["state"] == "REJECTED_BY_BROKER" and len(fake.places) == 1
    assert "Insufficient funds" in _outbox(pid).reject_reason


def test_021_late_acceptance_after_not_placed_is_reported_by_the_tracker(acct021):
    from countersign.executor.tracker import apply_order_update

    pid, fake = _approved_021(acct021, "not_placed")
    run_job(job_for(pid).id)
    ob = _outbox(pid)
    late = fake._order(dict(ob.request), "256399")
    with session_scope() as s:
        apply_order_update(s, late)
    ob = _outbox(pid)
    assert ob.state == "RECONCILED_FOUND" and ob.broker_order_id == "256399"
    assert "RECONCILED" in _outcomes(pid) and len(fake.places) == 1


# ------------------------------------------------------------------ every broker: three fixes


@pytest.fixture
def paper():
    fb = PaperFake()
    pipeline._brokers.clear()
    pipeline._brokers["LAB1"] = fb
    yield fb
    pipeline._brokers.clear()


def test_fix_a_5xx_after_acceptance_is_reconciled_not_rejected(paper, account):
    set_tick("NSE:EQ:INFY", 186000)
    pid = propose("buy 5 INFY at 1860", account)
    approve(pid, hold=True)
    paper.faults = ["accept_then_503"]
    out = run_job(job_for(pid).id)
    assert out["ok"] and out["send"]["state"] == "RECONCILED_FOUND" and len(paper.book) == 1


def _open_paper_order(paper, account) -> tuple[str, object]:
    from countersign.core.verifier import AccountState

    set_tick("NSE:EQ:INFY", 186000)
    pid = propose("buy 5 INFY at 1860", account)
    approve(pid, hold=True)
    run_job(job_for(pid).id)
    return paper.book[0]["broker_order_id"], AccountState.load(BookGateway(paper), "LAB1")


def test_fix_b_modify_with_lost_answer_is_confirmed_on_the_target_order(paper, account):
    oid, acc2 = _open_paper_order(paper, account)
    pid = propose("modify my INFY order to 1855", acc2)
    approve(pid, hold=True)
    paper.faults = ["modify_lost_applied"]
    out = run_job(job_for(pid).id)
    assert out["ok"] and out["send"]["state"] == "RECONCILED_FOUND" and out["send"]["broker_order_id"] == oid
    assert paper.modify_calls == 1 and paper.book[0]["limit_price_paise"] == 185500 and len(paper.book) == 1
    with session_scope() as s:
        assert s.get(Proposal, pid).status == "COMPLETED"


def test_fix_b_modify_not_applied_gets_one_safe_retry_on_paper(paper, account):
    oid, acc2 = _open_paper_order(paper, account)
    pid = propose("modify my INFY order to 1855", acc2)
    approve(pid, hold=True)
    paper.faults = ["modify_lost_not_applied"]
    out = run_job(job_for(pid).id)
    assert out["ok"] and paper.modify_calls == 2 and paper.book[0]["limit_price_paise"] == 185500


def test_fix_b_cancel_is_confirmed_on_the_target_order(paper, account):
    oid, acc2 = _open_paper_order(paper, account)
    pid = propose("cancel my INFY order", acc2)
    approve(pid, hold=True)
    out = run_job(job_for(pid).id)
    assert out["ok"] and out["send"]["broker_order_id"] == oid and paper.book[0]["status"] == "CANCELLED"


def _preflighted(paper, account) -> tuple[str, dict]:
    set_tick("NSE:EQ:INFY", 186000)
    pid = propose("buy 5 INFY at 1860", account)
    approve(pid, hold=True)
    jid = job_for(pid).id
    with session_scope() as s:
        s.execute(text("UPDATE exec_jobs SET status='PENDING' WHERE id=:id"), {"id": jid})
    pre = pipeline.preflight(jid)
    assert pre["ok"]
    pipeline._live_jobs.discard(jid)  # the executor crashed after preflight; DBOS replays the workflow
    return jid, pre["steps"][0]


def test_fix_c_recovery_never_sends_after_the_approval_expired(paper, account):
    jid, step = _preflighted(paper, account)
    with session_scope() as s:
        cap_appr = s.execute(select(Approval).join(ExecJob, ExecJob.proposal_id == Approval.proposal_id).where(ExecJob.id == jid)).scalar_one()
        s.execute(text("UPDATE approvals SET issued_at = now() - interval '10 minutes', expires_at = now() - interval '5 minutes' WHERE id=:id"), {"id": cap_appr.id})
    out = pipeline.send_step(jid, step)
    assert not out["ok"] and out["code"] == "APPROVAL_EXPIRED" and paper.book == []


def test_fix_c_recovery_never_sends_a_previous_day_order(paper, account):
    jid, step = _preflighted(paper, account)
    with session_scope() as s:
        s.execute(text("UPDATE exec_jobs SET started_at = now() - interval '1 day' WHERE id=:id"), {"id": jid})
    out = pipeline.send_step(jid, step)
    assert not out["ok"] and "previous trading day" in out["message"] and paper.book == []


def test_fix_c_recovered_send_in_flight_is_reconciled_but_not_resent_after_expiry(paper, account):
    jid, step = _preflighted(paper, account)
    pipeline._live_jobs.add(jid)
    paper.faults = ["unavailable", "unavailable"]
    # First attempt: connection refused, so the outbox is left UNKNOWN as if the executor died right after.
    with session_scope() as s:
        s.execute(text("UPDATE approvals SET expires_at = now() + interval '1 hour' WHERE proposal_id = (SELECT proposal_id FROM exec_jobs WHERE id=:id)"), {"id": jid})
    from countersign.core.schemas import CanonicalOrder

    order = CanonicalOrder.model_validate(step["order"])
    with session_scope() as s:
        job = s.get(ExecJob, jid)
        ob = OutboxEntry(job_id=jid, proposal_id=job.proposal_id, capability_id=job.capability_id, step_no=1, client_order_id=pipeline.client_order_id(job.capability_id, 1), idempotency_key=f"{job.capability_id}:1", correlation_id=jid, request=pipeline._request_body(order, pipeline.client_order_id(job.capability_id, 1)) | {"action": "PLACE", "target_broker_order_id": None}, request_hash="x", state="SENDING", attempts=1)
        s.add(ob)
        s.execute(text("UPDATE approvals SET expires_at = now() - interval '1 minute', issued_at = now() - interval '2 minutes' WHERE proposal_id=:p"), {"p": job.proposal_id})
    pipeline._live_jobs.discard(jid)
    paper.faults = []
    out = pipeline.send_step(jid, step)
    assert out["state"] == "FAILED" and paper.book == []  # reconciled (absent) and NOT resent: the approval expired


class Gateway021WithOrder(Gateway021Fake):
    """A 021 gateway fake holding one open order; cancel either confirms or loses its answer without applying."""

    def __init__(self, cancel_behaviour: str = "ok"):
        super().__init__("ok")
        self.cancel_behaviour = cancel_behaviour
        self.cancels = 0

    def cancel(self, oid: str) -> dict:
        self.cancels += 1
        if self.cancel_behaviour == "lost_not_applied":
            raise BrokerTimeout("read timed out")
        self.book[oid]["status"] = "CANCELLED"
        self.book[oid]["status_021"] = "Cancelled"
        return dict(self.book[oid]) | {"cancellation": "CONFIRMED"}


def _open_021_order(acct021, fake) -> object:
    from countersign.core.verifier import AccountState

    pipeline._brokers[ACCT] = fake
    set_tick("NSE:EQ:INFY", 186000)
    pid = propose("buy 5 INFY at 1860", acct021)
    approve(pid, hold=True)
    run_job(job_for(pid).id)

    class Book(FakeGateway):
        def orders(self, status="all", tag=None):
            return [dict(o) for o in fake.book.values() if status != "open" or o["status"] == "OPEN"]

    return AccountState.load(Book(), ACCT)


def test_021_cancel_is_confirmed_on_the_order(acct021):
    fake = Gateway021WithOrder("ok")
    acc = _open_021_order(acct021, fake)
    pid = propose("cancel my INFY order", acc)
    approve(pid, hold=True)
    out = run_job(job_for(pid).id)
    assert out["ok"] and out["send"]["broker_order_id"] == "256300" and fake.cancels == 1
    with session_scope() as s:
        assert s.get(Proposal, pid).status == "COMPLETED"


def test_021_unconfirmed_cancel_is_not_retried(acct021, monkeypatch):
    from countersign.core.config import settings

    monkeypatch.setattr(settings(), "reconcile_window_s", 0.6)
    fake = Gateway021WithOrder("lost_not_applied")
    acc = _open_021_order(acct021, fake)
    pid = propose("cancel my INFY order", acc)
    approve(pid, hold=True)
    out = run_job(job_for(pid).id)
    assert out["send"]["state"] == "NOT_PLACED" and fake.cancels == 1
    assert "still shows its previous state" in _outbox(pid).reject_reason


def test_021_approver_removed_from_the_owner_list_cannot_execute(acct021, monkeypatch):
    from countersign.core.config import settings

    pid, fake = _approved_021(acct021, "ok")
    monkeypatch.setattr(settings(), "owner_emails_021", "someone-else@example.invalid")
    out = run_job(job_for(pid).id)
    assert not out["ok"] and out["code"] == "PAYLOAD_MISMATCH" and fake.places == []


def test_a_fill_after_a_confirmed_cancellation_is_reported_plainly(acct021):
    from countersign.executor.tracker import apply_order_update

    pid, fake = _approved_021(acct021, "ok")
    run_job(job_for(pid).id)
    o = fake.book["256300"]
    with session_scope() as s:
        apply_order_update(s, o | {"status": "CANCELLED"})
    with session_scope() as s:
        apply_order_update(s, o | {"status": "COMPLETE", "filled_quantity": 5, "avg_fill_price_paise": 186000})
    with session_scope() as s:
        titles = [r.title for r in s.execute(select(OutcomeRow).where(OutcomeRow.proposal_id == pid)).scalars()]
    assert any(t.startswith("Filled after its cancellation") for t in titles)
