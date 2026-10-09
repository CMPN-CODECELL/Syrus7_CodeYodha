"""Tamper-evident, append-only audit log. hash_n = sha256(hash_{n-1} || RFC 8785(event_n)).
Inserts are serialized with a transaction-scoped advisory lock so the chain is strictly linear."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from countersign.core.canonical import jcs, sha256_hex
from countersign.core.models import AuditEvent

GENESIS = "0" * 64
_AUDIT_LOCK = 4242
_SECRET_KEYS = {"pin", "otp", "totp", "code", "secret", "private_key", "api_key", "write_key", "token", "password"}


def _scrub(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("[redacted]" if k.lower() in _SECRET_KEYS else _scrub(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_scrub(v) for v in data]
    return data


def _event_body(ts: datetime, actor: str, event_type: str, subject_type: str, subject_id: str, data: dict) -> dict:
    return {
        "ts": ts.astimezone(UTC).isoformat(),
        "actor": actor,
        "event_type": event_type,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "data": data,
    }


def record(
    s: Session,
    actor: str,
    event_type: str,
    subject_type: str,
    subject_id: str,
    data: dict | None = None,
) -> AuditEvent:
    s.execute(text("select pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK})
    prev = s.execute(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1)).scalar()
    prev_hash = prev or GENESIS
    ts = datetime.now(UTC).replace(microsecond=(datetime.now(UTC).microsecond // 1000) * 1000)
    clean = _scrub(data or {})
    body = _event_body(ts, actor, event_type, subject_type, str(subject_id), clean)
    h = sha256_hex(prev_hash.encode() + jcs(body))
    ev = AuditEvent(
        ts=ts,
        actor=actor,
        event_type=event_type,
        subject_type=subject_type,
        subject_id=str(subject_id),
        data=clean,
        prev_hash=prev_hash,
        hash=h,
    )
    s.add(ev)
    s.flush()
    return ev


def verify_chain(s: Session) -> dict:
    prev_hash = GENESIS
    checked = 0
    for ev in s.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars():
        body = _event_body(ev.ts, ev.actor, ev.event_type, ev.subject_type, ev.subject_id, ev.data)
        expected = sha256_hex(prev_hash.encode() + jcs(body))
        if ev.prev_hash != prev_hash or ev.hash != expected:
            return {
                "ok": False,
                "checked": checked,
                "head_hash": prev_hash,
                "broken_at_id": ev.id,
                "detail": f"Tampering detected at event #{ev.id} ({ev.event_type}): stored hash does not match its contents and predecessor.",
            }
        prev_hash = ev.hash
        checked += 1
    return {
        "ok": True,
        "checked": checked,
        "head_hash": prev_hash,
        "broken_at_id": None,
        "detail": f"Chain intact: {checked} events verified from genesis to head.",
    }
