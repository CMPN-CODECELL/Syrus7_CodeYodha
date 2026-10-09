"""The real executor pipeline against a fault-injecting fake broker: idempotency, reconciliation, replay, expiry,
tampering, price drift, kill switch, plan immutability."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from countersign.core import state
from countersign.core.db import session_scope
from countersign.core.models import ExecJob
from tests.conftest import set_tick
from tests.helpers import approve, job_for, propose, run_job


def _order(account):
    set_tick("NSE:EQ:INFY", 186000)
    pid = propose("buy 5 INFY at 1860", account)
    approve(pid, hold=True)
    return pid


def test_happy_path_sends_exactly_one_order(fake_broker, account):
    pid = _order(account)
    out = run_job(job_for(pid).id)
    assert out["ok"] and len(fake_broker.book) == 1
    assert fake_broker.book[0]["quantity"] == 5 and fake_broker.book[0]["client_order_id"].startswith("CS")


def test_timeout_after_accept_reconciles_without_duplicate(fake_broker, account):
    pid = _order(account)
    fake_broker.faults = ["accept_then_timeout"]
    out = run_job(job_for(pid).id)
    assert out["ok"] and out["send"]["state"] == "RECONCILED_FOUND"
    assert len(fake_broker.book) == 1


def test_connection_refused_retries_once_safely(fake_broker, account):
    pid = _order(account)
    fake_broker.faults = ["unavailable"]
    out = run_job(job_for(pid).id)
    assert out["ok"] and len(fake_broker.book) == 1


def test_replay_of_used_capability_is_rejected(fake_broker, account):
    pid = _order(account)
    j = job_for(pid)
    run_job(j.id)
    with session_scope() as s:
        r = ExecJob(capability_id=j.capability_id, kind="ORDER", proposal_id=pid, status="PENDING", source="test")
        s.add(r)
        s.flush()
        rid = r.id
    out = run_job(rid)
    assert not out["ok"] and out["code"] == "CAPABILITY_REPLAY"
    assert len(fake_broker.book) == 1


def test_expired_approval_never_executes(fake_broker, account):
    set_tick("NSE:EQ:INFY", 186000)
    pid = propose("buy 5 INFY at 1860", account)
    approve(pid, hold=True, issued_at=datetime.now(UTC) - timedelta(seconds=56))
    import time

    time.sleep(4.5)
    set_tick("NSE:EQ:INFY", 186000)
    out = run_job(job_for(pid).id)
    assert not out["ok"] and out["code"] == "APPROVAL_EXPIRED" and fake_broker.book == []


def test_tampered_order_and_payload_are_refused(fake_broker, account):
    pid = _order(account)
    with session_scope() as s:
        s.execute(text("UPDATE proposals SET canonical = jsonb_set(canonical, '{quantity}', '500') WHERE id=:id"), {"id": pid})
    out = run_job(job_for(pid).id)
    assert not out["ok"] and out["code"] == "TAMPERED_ORDER"
    pid2 = _order(account)
    with session_scope() as s:
        s.execute(text("UPDATE approvals SET payload = jsonb_set(payload, '{price_refs,0,band_high_paise}', '999999999') WHERE proposal_id=:id"), {"id": pid2})
    out2 = run_job(job_for(pid2).id)
    assert not out2["ok"] and out2["code"] == "PAYLOAD_MISMATCH"
    assert fake_broker.book == []


def test_price_drift_after_approval_blocks_execution(fake_broker, account):
    pid = _order(account)
    set_tick("NSE:EQ:INFY", 186000 * 103 // 100)
    out = run_job(job_for(pid).id)
    assert out["ok"] is True and out["send"]["ok"] is False and out["send"]["code"] == "PRICE_DRIFT"
    assert fake_broker.book == []


def test_kill_switch_halts_execution(fake_broker, account):
    pid = _order(account)
    with session_scope() as s:
        state.set_kill_switch(s, True, "test", "test")
    out = run_job(job_for(pid).id)
    assert not out["ok"] and out["code"] == "KILL_SWITCH_ENGAGED" and fake_broker.book == []


def test_plan_needs_one_signature_and_is_immutable(fake_broker, account):
    from countersign.core import proposals
    from countersign.core.parser import parse
    from tests.helpers import lab_user_id

    set_tick("NSE:EQ:INFY", 186000)
    set_tick("NSE:EQ:TCS", 410000)
    text_ = "sell 20 INFY at 1860 and buy 2 TCS at 4100"
    with session_scope() as s:
        out = proposals.create_plan(s, parse(text_).plan, user_text=text_, account=account, user_id=lab_user_id(), engine="deterministic", origin="test")
        pid = out.proposal.id
        assert out.proposal.status == "AWAITING_APPROVAL" and len(out.proposal.steps_view) == 2
    approve(pid, hold=True)
    with session_scope() as s:
        s.execute(text("UPDATE proposals SET canonical = jsonb_set(canonical, '{steps,1,order,quantity}', '200') WHERE id=:id"), {"id": pid})
    res = run_job(job_for(pid).id)
    assert not res["ok"] and res["code"] in ("TAMPERED_ORDER", "PLAN_MODIFIED")
    assert fake_broker.book == []
