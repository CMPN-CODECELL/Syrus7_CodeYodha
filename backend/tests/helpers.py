from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from countersign.auth import countersign as cs
from countersign.auth import virtual_authenticator as vauth
from countersign.core import proposals
from countersign.core.approval_crypto import challenge_bytes
from countersign.core.db import session_scope
from countersign.core.models import DeviceCredential, ExecJob, Proposal, User
from countersign.core.parser import parse


def lab_user_id() -> str:
    with session_scope() as s:
        return s.execute(select(User.id).where(User.username == "attack-lab")).scalar_one()


def propose(text_: str, account) -> str:
    p = parse(text_)
    with session_scope() as s:
        out = proposals.create_order(s, p.order, user_text=text_, account=account, user_id=lab_user_id(), engine="deterministic", origin="test")
        assert out.proposal is not None, out.clarify
        return out.proposal.id


def approve(pid: str, *, hold: bool = False, issued_at: datetime | None = None) -> None:
    uid = lab_user_id()
    va = vauth.get()
    with session_scope() as s:
        st = cs.start(s, s.get(User, uid), pid, "webauthn", issued_at=issued_at)
    with session_scope() as s:
        cred = s.execute(select(DeviceCredential).where(DeviceCredential.credential_id == va.credential_id)).scalar_one()
        assertion = va.assert_challenge(challenge_bytes(st["payload"]), sign_count=cred.sign_count)
    with session_scope() as s:
        cs.complete(s, s.get(User, uid), pid, st["approval_id"], webauthn=assertion, hold_job=hold)


def job_for(pid: str) -> ExecJob:
    with session_scope() as s:
        return s.execute(select(ExecJob).where(ExecJob.proposal_id == pid).order_by(ExecJob.created_at.desc())).scalars().first()


def run_job(job_id: str) -> dict:
    """Runs the real executor pipeline synchronously (the same functions the DBOS workflow calls)."""
    from sqlalchemy import text

    from countersign.executor import pipeline

    with session_scope() as s:
        s.execute(text("UPDATE exec_jobs SET status='PENDING' WHERE id=:id AND status='HELD'"), {"id": job_id})
    pre = pipeline.preflight(job_id)
    if not pre.get("ok"):
        return pre
    out = None
    for step in pre["steps"]:
        out = pipeline.send_step(job_id, step)
        if not out.get("ok"):
            break
    pipeline.finalize(job_id)
    return {"ok": True, "send": out}


def proposal(pid: str) -> Proposal:
    with session_scope() as s:
        return s.get(Proposal, pid)
