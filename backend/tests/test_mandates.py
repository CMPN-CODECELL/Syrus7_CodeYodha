"""Standing mandates: fire exactly once under concurrent triggers, survive restarts, never fire stale."""

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from countersign.core import proposals
from countersign.core.db import session_scope
from countersign.core.mandate_engine import Engine, evaluate
from countersign.core.market import Tick
from countersign.core.models import ExecJob, StandingMandate
from countersign.core.parser import parse
from tests.conftest import set_tick
from tests.helpers import approve, lab_user_id


def _armed_mandate(account) -> str:
    set_tick("NSE:EQ:INFY", 186000)
    text_ = "Buy 10 INFY if the price falls below 1800"
    with session_scope() as s:
        out = proposals.create_mandate(s, parse(text_).mandate, user_text=text_, account=account, user_id=lab_user_id(), engine="deterministic", origin="test")
        pid = out.proposal.id
    approve(pid)
    with session_scope() as s:
        m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == pid)).scalar_one()
        assert m.state == "ACTIVE"
        return m.id


def _tick(ltp: int, seq: int, ts: datetime | None = None) -> Tick:
    return Tick(instrument_id="NSE:EQ:INFY", symbol="INFY", ltp_paise=ltp, bid_paise=ltp - 5, ask_paise=ltp + 5, volume=1, prev_close_paise=186000, ts=ts or datetime.now(UTC), seq=seq)


def test_concurrent_triggers_fire_exactly_once(account):
    mid = _armed_mandate(account)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        with session_scope() as s:
            m = s.get(StandingMandate, mid)
            results.append(evaluate(s, m, _tick(179000 - i * 5, 100 + i), engine_id=f"engine-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("TRIGGERED") == 1
    with session_scope() as s:
        jobs = s.execute(select(ExecJob).where(ExecJob.mandate_id == mid)).scalars().all()
        assert len(jobs) == 1


def test_restart_does_not_refire(account):
    mid = _armed_mandate(account)
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, mid), _tick(179000, 1)) == "TRIGGERED"
    fresh_engine = Engine("engine-after-restart")
    fresh_engine.refresh()
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, mid), _tick(178000, 2)) == "NONE"
        assert len(s.execute(select(ExecJob).where(ExecJob.mandate_id == mid)).scalars().all()) == 1


def test_condition_crossed_during_downtime_goes_to_review(account):
    mid = _armed_mandate(account)
    with session_scope() as s:
        s.execute(text("UPDATE mandates SET last_evaluated_at = :ts WHERE id = :id"), {"ts": datetime.now(UTC) - timedelta(minutes=5), "id": mid})
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, mid), _tick(179000, 2)) == "NEEDS_REVIEW"
        assert s.get(StandingMandate, mid).state == "NEEDS_REVIEW"
        assert s.execute(select(ExecJob).where(ExecJob.mandate_id == mid)).scalars().all() == []


def test_mandate_scope_cannot_be_widened(fake_broker, account):
    mid = _armed_mandate(account)
    with session_scope() as s:
        evaluate(s, s.get(StandingMandate, mid), _tick(179000, 1))
        job = s.execute(select(ExecJob).where(ExecJob.mandate_id == mid)).scalar_one()
        s.execute(text("UPDATE exec_jobs SET order_request = jsonb_set(order_request, '{quantity}', '1000') WHERE id=:id"), {"id": job.id})
        jid = job.id
    set_tick("NSE:EQ:INFY", 179000)
    from tests.helpers import run_job

    out = run_job(jid)
    assert not out["ok"] and out["code"] == "MANDATE_SCOPE_VIOLATION"
    assert fake_broker.book == []
