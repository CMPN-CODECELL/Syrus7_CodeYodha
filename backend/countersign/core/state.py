"""Kill switch, outcomes and security events (persisted, audited, and pushed to the UI)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from countersign.core import audit, events
from countersign.core.models import Approval, BrokerOrderMirror, ExecJob, OutboxEntry, OutcomeRow, Proposal, SecurityEvent, SystemState
from countersign.core.schemas import iso

KILL = "kill_switch"


def kill_switch(s: Session) -> dict:
    row = s.get(SystemState, KILL)
    if row is None:
        return {"engaged": False, "reason": None, "at": None}
    return row.value


def set_kill_switch(s: Session, engaged: bool, reason: str, actor: str) -> dict:
    val = {"engaged": engaged, "reason": reason if engaged else None, "at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
    row = s.get(SystemState, KILL)
    if row is None:
        s.add(SystemState(key=KILL, value=val))
    else:
        row.value = val
    audit.record(s, actor, "KILL_SWITCH_ENGAGED" if engaged else "KILL_SWITCH_RELEASED", "system", "kill_switch", {"reason": reason})
    s.flush()
    outcome(s, "KILL_SWITCH", "Kill switch engaged" if engaged else "Kill switch released", reason, "danger" if engaged else "info", account_id="*")
    return val


def release_kill_switch_if(s: Session, expected: dict, reason: str, actor: str) -> bool:
    """Compare-and-set release: only when the switch still holds exactly the engagement the caller observed.
    Anyone else's later engagement (a trader, or the monitor for another order) is left in place."""
    row = s.get(SystemState, KILL, with_for_update=True, populate_existing=True)
    if row is None or not row.value.get("engaged") or row.value != expected:
        return False
    set_kill_switch(s, False, reason, actor)
    return True


def outcome(
    s: Session,
    kind: str,
    title: str,
    detail: str,
    severity: str,
    *,
    proposal_id: str | None = None,
    broker_order_id: str | None = None,
    account_id: str | None = None,
) -> None:
    if account_id is None:
        # Owned by the proposal's account; "*" (visible to every trader) only for system-wide outcomes.
        from countersign.core.models import Proposal

        p = s.get(Proposal, proposal_id) if proposal_id else None
        account_id = p.account_id if p is not None else "*"
    row = OutcomeRow(kind=kind, title=title, detail=detail, severity=severity, proposal_id=proposal_id, broker_order_id=broker_order_id, account_id=account_id)
    s.add(row)
    s.flush()
    events.emit(
        "outcome",
        {
            "id": row.id,
            "ts": iso(datetime.now(UTC)),
            "kind": kind,
            "title": title,
            "detail": detail,
            "severity": severity,
            "proposal_id": proposal_id,
            "broker_order_id": broker_order_id,
            "account_id": account_id,
        },
    )


LAB_ACCOUNT = "LAB1"


def _owner_account(s: Session, data: dict) -> str | None:
    """The account an event concerns, from what it references. None means system-wide (for example the audit chain)."""
    if data.get("account_id"):
        return data["account_id"]
    pid = data.get("proposal_id")
    if not pid and data.get("approval_id"):
        a = s.get(Approval, data["approval_id"])
        pid = a.proposal_id if a else None
    if not pid and data.get("job_id"):
        j = s.get(ExecJob, data["job_id"])
        pid = j.proposal_id if j else None
    if not pid and data.get("tag"):
        pid = s.execute(select(OutboxEntry.proposal_id).where(OutboxEntry.client_order_id == data["tag"])).scalars().first()
    if pid:
        p = s.get(Proposal, pid)
        return p.account_id if p else None
    if data.get("broker_order_id"):
        m = s.get(BrokerOrderMirror, data["broker_order_id"])
        return m.account_id if m else None
    return None


def security_event(s: Session, severity: str, kind: str, message: str, data: dict | None = None, actor: str = "system", account_id: str | None = None) -> SecurityEvent:
    data = data or {}
    acct = account_id or _owner_account(s, data)
    # Informational events need no action. A refusal on the shared Attack Lab sandbox is a drill that was blocked as
    # designed, so it is not left open; out-of-band orders stay open until the drill that placed them marks them contained.
    resolved = severity == "info" or (acct == LAB_ACCOUNT and kind != "UNAUTHORIZED_BROKER_ORDER")
    ev = SecurityEvent(severity=severity, kind=kind, message=message, data=data, account_id=acct, resolved=resolved)
    s.add(ev)
    s.flush()
    audit.record(s, actor, "SECURITY_EVENT", "security", ev.id, {"kind": kind, "severity": severity, "message": message})
    events.emit("security", {"id": ev.id, "ts": iso(datetime.now(UTC)), "severity": severity, "kind": kind, "message": message, "data": data, "resolved": resolved, "account_id": acct, "drill": acct == LAB_ACCOUNT})
    return ev


def get_state(s: Session, key: str, default: dict | None = None) -> dict:
    row = s.get(SystemState, key)
    return row.value if row else (default or {})


def put_state(s: Session, key: str, value: dict) -> None:
    row = s.get(SystemState, key)
    if row is None:
        s.add(SystemState(key=key, value=value))
    else:
        row.value = value
