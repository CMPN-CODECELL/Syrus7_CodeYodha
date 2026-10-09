"""Hypothesis stateful test of the whole authorization boundary.

Random interleavings of: approve orders, execute (with broker faults), replay capabilities, tamper with orders and
payloads, let approvals expire, and move the market. After every step the invariants must hold:
  I1  every order at the broker maps to a consumed capability backed by a VERIFIED approval of that exact hash
  I2  at most one broker order per client tag (no duplicates, even after timeouts)
  I3  a capability backs at most one broker order"""

from collections import Counter
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, invariant, multiple, rule

from countersign.core.approval_crypto import CountersignError
from sqlalchemy import select, text

from countersign.core.canonical import canonical_hash
from countersign.core.db import session_scope
from countersign.core.models import Approval, Capability, ExecJob, OutboxEntry, Proposal
from tests.conftest import FakeBroker, FakeGateway, set_tick
from tests.helpers import approve, job_for, propose, run_job


class AuthorizationBoundary(RuleBasedStateMachine):
    jobs = Bundle("jobs")

    @initialize()
    def setup(self):
        from countersign.core.verifier import AccountState
        from countersign.executor import pipeline

        self.broker = FakeBroker()
        pipeline._brokers.clear()
        pipeline._brokers["LAB1"] = self.broker
        self.account = AccountState.load(FakeGateway(), "LAB1")
        self.base = 186000
        set_tick("NSE:EQ:INFY", self.base)

    def _fresh(self):
        set_tick("NSE:EQ:INFY", self.base)

    @rule(target=jobs, qty=st.integers(1, 20), backdate=st.sampled_from([0, 0, 0, 61]))
    def approve_order(self, qty, backdate):
        self._fresh()
        pid = propose(f"buy {qty} INFY at 1860", self.account)
        try:
            approve(pid, hold=True, issued_at=datetime.now(UTC) - timedelta(seconds=backdate) if backdate else None)
        except CountersignError as e:
            assert e.code == "APPROVAL_EXPIRED"
            return multiple()
        return job_for(pid).id

    @rule(job=jobs, fault=st.sampled_from([None, None, "accept_then_timeout", "unavailable"]))
    def execute(self, job, fault):
        self._fresh()
        if fault:
            self.broker.faults = [fault]
        run_job(job)
        self.broker.faults = []

    @rule(job=jobs)
    def replay(self, job):
        with session_scope() as s:
            j = s.get(ExecJob, job)
            r = ExecJob(capability_id=j.capability_id, kind=j.kind, proposal_id=j.proposal_id, status="PENDING", source="replay")
            s.add(r)
            s.flush()
            rid = r.id
        self._fresh()
        run_job(rid)

    @rule(job=jobs, new_qty=st.integers(21, 5000))
    def tamper_order(self, job, new_qty):
        with session_scope() as s:
            j = s.get(ExecJob, job)
            s.execute(text("UPDATE proposals SET canonical = jsonb_set(canonical, '{quantity}', to_jsonb(CAST(:q AS int))) WHERE id=:id"), {"q": new_qty, "id": j.proposal_id})
        self._fresh()
        run_job(job)

    @rule(job=jobs)
    def drift_then_execute(self, job):
        set_tick("NSE:EQ:INFY", self.base * 104 // 100)
        run_job(job)
        self._fresh()

    @invariant()
    def every_broker_order_is_countersigned(self):
        if not hasattr(self, "broker"):
            return
        with session_scope() as s:
            for o in self.broker.book:
                ob = s.execute(select(OutboxEntry).where(OutboxEntry.client_order_id == o["client_order_id"])).scalar_one()
                cap = s.get(Capability, ob.capability_id)
                appr = s.get(Approval, cap.approval_id)
                p = s.get(Proposal, ob.proposal_id)
                assert cap.status == "USED" and appr.status == "VERIFIED"
                assert cap.canonical_hash == appr.payload["canonical_hash"]
                assert o["quantity"] == ob.request["quantity"]
                assert ob.request_hash == cap.canonical_hash, "sent order differs from the countersigned order"

    @invariant()
    def no_duplicate_tags_or_capability_reuse(self):
        if not hasattr(self, "broker"):
            return
        tags = Counter(o["client_order_id"] for o in self.broker.book)
        assert all(n == 1 for n in tags.values()), tags
        with session_scope() as s:
            rows = s.execute(select(OutboxEntry.capability_id).where(OutboxEntry.client_order_id.in_(list(tags)))).scalars().all() if tags else []
            caps = Counter(rows)
            assert all(n == 1 for n in caps.values()), caps

    def teardown(self):
        from countersign.executor import pipeline

        pipeline._brokers.clear()


AuthorizationBoundary.TestCase.settings = settings(
    max_examples=15, stateful_step_count=10, deadline=None, suppress_health_check=list(HealthCheck)
)
TestAuthorizationBoundary = AuthorizationBoundary.TestCase
