"""Multi-user correctness: one trader can never read, approve, reject or reset another trader's data, and the live
event stream only carries a trader's own proposals, mandates and outcomes."""

from sqlalchemy import select, text

from countersign.auth import countersign as cs
from countersign.auth import factors
from countersign.core import events, state
from countersign.core.db import session_scope
from countersign.core.models import BrokerOrderMirror, ChatMessage, OutcomeRow, Proposal, User
from countersign.executor import pipeline
from tests.accounts_support import csrf, propose_for, signed_up
from tests.conftest import set_tick
from tests.helpers import job_for


def test_user_b_cannot_read_reject_or_approve_user_a_proposal(api):
    main, _ = api
    a, me_a = signed_up(main)
    b, _ = signed_up(main)
    pid = propose_for(me_a["user"]["id"], me_a["account"]["id"])
    assert a.get(f"/api/proposals/{pid}").status_code == 200
    assert [p["id"] for p in a.get("/api/proposals").json()] == [pid]

    assert b.get(f"/api/proposals/{pid}").status_code == 404
    assert b.get("/api/proposals").json() == []
    assert b.post(f"/api/proposals/{pid}/reject", headers=csrf(b)).status_code == 404
    assert b.post(f"/api/proposals/{pid}/approval/start", json={"method": "pin"}, headers=csrf(b)).status_code == 404

    assert a.post("/api/auth/pin", json={"pin": "4826"}, headers=csrf(a)).status_code == 200
    assert b.post("/api/auth/pin", json={"pin": "4826"}, headers=csrf(b)).status_code == 200
    start = a.post(f"/api/proposals/{pid}/approval/start", json={"method": "pin"}, headers=csrf(a))
    assert start.status_code == 200, start.text
    stolen = b.post(f"/api/proposals/{pid}/approval/complete", json={"approval_id": start.json()["approval_id"], "pin": "4826"}, headers=csrf(b))
    assert stolen.status_code == 404
    with session_scope() as s:
        assert s.get(Proposal, pid).status == "AWAITING_APPROVAL"


def test_account_and_history_endpoints_use_the_callers_own_account(api, monkeypatch):
    main, _ = api
    a, me_a = signed_up(main)
    b, me_b = signed_up(main)
    seen: list[str] = []

    class Gw:
        def __init__(self, acct):
            self.acct = acct

        def holdings(self):
            seen.append(self.acct)
            return []

    monkeypatch.setattr(main, "read_gateway", lambda acct: Gw(acct))
    a.get("/api/account/holdings")
    b.get("/api/account/holdings")
    assert seen == [me_a["account"]["id"], me_b["account"]["id"]]

    with session_scope() as s:
        s.add(ChatMessage(conversation_id="conv-a", user_id=me_a["user"]["id"], role="user", text="private note", cards=[]))
    assert b.get("/api/chat/history", params={"conversation_id": "conv-a"}).json() == []
    r = b.post("/api/chat", json={"conversation_id": "conv-a", "message": "hello", "engine": "deterministic"}, headers=csrf(b))
    assert r.status_code == 404  # another trader's conversation id is refused, so its model memory can never load


def test_events_carry_owner_and_stream_filter_hides_other_traders(api, published):
    main, _ = api
    _, me_a = signed_up(main)
    _, me_b = signed_up(main)
    pid = propose_for(me_a["user"]["id"], me_a["account"]["id"])
    with session_scope() as s:
        state.outcome(s, "ORDER_FILLED", "Filled", "test", "success", proposal_id=pid)
        state.set_kill_switch(s, True, "test", "trader")
    published = [payload for channel, payload in published if channel == events.CH_EVENTS]
    prop = next(e for e in published if e["type"] == "proposal")
    assert prop["data"]["user_id"] == me_a["user"]["id"] and prop["data"]["account_id"] == me_a["account"]["id"]
    outcomes = [e for e in published if e["type"] == "outcome"]
    filled = next(e for e in outcomes if e["data"]["kind"] == "ORDER_FILLED")
    assert filled["data"]["account_id"] == me_a["account"]["id"]  # resolved from the proposal, never a default
    kill = next(e for e in outcomes if e["data"]["kind"] == "KILL_SWITCH")

    a_id, a_acct = me_a["user"]["id"], me_a["account"]["id"]
    b_id, b_acct = me_b["user"]["id"], me_b["account"]["id"]
    for ev in (prop, filled):
        assert events.visible_to(ev["type"], ev["data"], a_id, a_acct)
        assert not events.visible_to(ev["type"], ev["data"], b_id, b_acct)
    assert events.visible_to("outcome", kill["data"], b_id, b_acct)  # system-wide outcomes reach everyone
    assert events.visible_to("tick", {"instrument_id": "NSE:EQ:INFY"}, b_id, b_acct)
    assert events.visible_to("security", {"kind": "X"}, b_id, b_acct)
    assert not events.visible_to("mandate", {"mandate_id": "m1"}, b_id, b_acct)  # owned type without owner: dropped
    assert not events.visible_to("mandate", {"mandate_id": "m1", "user_id": a_id, "account_id": a_acct}, b_id, b_acct)


def test_audit_list_shows_own_events_and_system_events_only(api):
    main, _ = api
    a, me_a = signed_up(main)
    b, _ = signed_up(main)
    pid = propose_for(me_a["user"]["id"], me_a["account"]["id"])
    with session_scope() as s:
        state.security_event(s, "warning", "TEST_EVENT", "system-wide test event")
    a_rows = a.get("/api/audit", params={"limit": 200}).json()
    b_rows = b.get("/api/audit", params={"limit": 200}).json()
    assert any(r["subject_type"] == "proposal" and r["subject_id"] == pid for r in a_rows)
    assert not any(r["subject_id"] == pid for r in b_rows)
    assert not any(r["subject_type"] == "user" and r["subject_id"] == me_a["user"]["id"] for r in b_rows)
    assert any(r["subject_type"] == "security" for r in b_rows) and any(r["subject_type"] == "security" for r in a_rows)
    assert b.get("/api/audit/verify").json()["ok"] is True


def test_public_status_shape_has_no_personal_data(api):
    main, _ = api
    signed_up(main)
    set_tick("NSE:IDX:NIFTY", 2_451_230_0, prev=2_440_000_0)
    from fastapi.testclient import TestClient

    r = TestClient(main.app).get("/api/public/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"all_operational", "systems", "unapproved_executions", "attacks_attempted", "attacks_blocked", "last_attack_run_at", "audit_events", "market", "generated_at"}
    assert set(body["systems"]) == {"api", "broker", "feed", "executor", "monitor"}
    assert body["systems"]["api"] is True and body["market"]["mode"] == "replay"
    assert body["market"]["nifty"] == {"ltp_paise": 2_451_230_0, "change_bps": int((2_451_230_0 - 2_440_000_0) * 10000 / 2_440_000_0)}
    assert isinstance(body["audit_events"], int) and body["audit_events"] > 0
    flat = r.text.lower()
    for word in ("email", "user_id", "username", "display_name", "account_id", "session"):
        assert word not in flat


def test_demo_reset_only_touches_the_callers_data(api, published):
    main, admin = api
    a, me_a = signed_up(main)
    b, me_b = signed_up(main)
    a_id, a_acct = me_a["user"]["id"], me_a["account"]["id"]
    b_id, b_acct = me_b["user"]["id"], me_b["account"]["id"]
    pa = propose_for(a_id, a_acct)
    pb = propose_for(b_id, b_acct)
    with session_scope() as s:
        for acct, oid in ((a_acct, "OA1"), (b_acct, "OB1")):
            s.add(BrokerOrderMirror(broker_order_id=oid, account_id=acct, instrument_id="NSE:EQ:INFY", side="BUY", quantity=5, status="OPEN"))
        s.add(ChatMessage(conversation_id="ca", user_id=a_id, role="user", text="a", cards=[]))
        s.add(ChatMessage(conversation_id="cb", user_id=b_id, role="user", text="b", cards=[]))
    admin.calls.clear()
    r = a.post("/api/demo/reset", headers=csrf(a))
    assert r.status_code == 200 and r.json()["account_id"] == a_acct
    assert ("reset", a_acct) in admin.calls and ("reset", b_acct) not in admin.calls
    assert ("fault", "none") in admin.calls and ("replenish", "LAB1") in admin.calls and ("replenish", "NAIVE1") in admin.calls
    assert (events.CH_FEED_CONTROL, {"cmd": "scenario", "name": "calm"}) in published
    with session_scope() as s:
        assert s.get(Proposal, pa) is None and s.get(Proposal, pb) is not None
        assert s.get(BrokerOrderMirror, "OA1") is None and s.get(BrokerOrderMirror, "OB1") is not None
        assert s.execute(select(OutcomeRow).where(OutcomeRow.account_id == a_acct)).first() is None
        assert s.execute(select(ChatMessage).where(ChatMessage.user_id == b_id)).first() is not None
        assert s.execute(select(ChatMessage).where(ChatMessage.user_id == a_id)).first() is None
    assert b.get(f"/api/proposals/{pb}").status_code == 200


def test_executor_refuses_an_order_for_an_account_the_approver_does_not_own(api):
    main, _ = api
    _, me_a = signed_up(main)
    _, me_b = signed_up(main)
    # A proposal for user A that (by a bug or tampering) names user B's account.
    pid = propose_for(me_a["user"]["id"], me_b["account"]["id"])
    set_tick("NSE:EQ:INFY", 186000)
    with session_scope() as s:
        u = s.get(User, me_a["user"]["id"])
        factors.set_pin(s, u, "4826")
    with session_scope() as s:
        st = cs.start(s, s.get(User, me_a["user"]["id"]), pid, "pin")
    with session_scope() as s:
        cs.complete(s, s.get(User, me_a["user"]["id"]), pid, st["approval_id"], pin="4826", hold_job=True)
    job = job_for(pid)
    with session_scope() as s:
        s.execute(text("UPDATE exec_jobs SET status='PENDING' WHERE id=:id"), {"id": job.id})
    out = pipeline.preflight(job.id)
    assert out["ok"] is False and out["code"] == "PAYLOAD_MISMATCH" and "does not own" in out["message"]
