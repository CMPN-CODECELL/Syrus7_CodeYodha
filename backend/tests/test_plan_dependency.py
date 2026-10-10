"""A dependent plan step runs once the step before it has filled, fully or partly, and the executor asks the broker
directly instead of waiting on an order update that may never arrive (021's sandbox holds part of an order)."""

from sqlalchemy import text

from countersign.core import proposals
from countersign.core.db import session_scope
from countersign.core.parser import parse
from countersign.executor import pipeline
from tests.helpers import approve, job_for
from tests.test_l2_l4_support import Gw, account, holding, ltp, user_id


def _sent_step_one(fake_broker) -> str:
    r, _ = ltp("RELIANCE")
    ltp("TCS")
    text_ = "Sell 5 RELIANCE at market and then buy 3 TCS at market"
    with session_scope() as s:
        out = proposals.create_plan(s, parse(text_).plan, user_text=text_, account=account(Gw(holdings=[holding("RELIANCE", 9, r)])), user_id=user_id(), engine="deterministic", origin="test")
        pid = out.proposal.id
    approve(pid, hold=True)
    job = job_for(pid)
    with session_scope() as s:
        s.execute(text("UPDATE exec_jobs SET status='PENDING' WHERE id=:id"), {"id": job.id})
    pre = pipeline.preflight(job.id)
    assert pre["ok"], pre
    assert pre["steps"][1]["depends_on"] == 1
    assert pipeline.send_step(job.id, pre["steps"][0])["ok"]
    return job.id


def test_a_partly_filled_step_lets_the_next_step_run(fake_broker):
    job_id = _sent_step_one(fake_broker)
    fake_broker.book[0].update(status="PARTIALLY_FILLED", filled_quantity=3, pending_quantity=2)  # only the broker knows
    assert pipeline.wait_for_fill(job_id, 1, timeout_s=5) == {"filled": True, "partial": True}


def test_a_fill_only_the_broker_reports_is_found_by_asking_it(fake_broker):
    job_id = _sent_step_one(fake_broker)
    fake_broker.book[0].update(status="COMPLETE", filled_quantity=5, pending_quantity=0)
    assert pipeline.wait_for_fill(job_id, 1, timeout_s=5) == {"filled": True}


def test_a_step_that_fills_nothing_stops_the_plan(fake_broker):
    job_id = _sent_step_one(fake_broker)
    w = pipeline.wait_for_fill(job_id, 1, timeout_s=2)
    assert w["filled"] is False and "did not fill within 2s" in w["reason"]
