"""Single-use, expiring execution capabilities. Minted only after a verified countersignature; consumed by the
executor with an atomic compare-and-set, so a capability can never be used twice or after expiry."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from countersign.core.models import Approval, Capability, Proposal


def mint(s: Session, approval: Approval, proposal: Proposal, *, expires_at: datetime | None = None) -> Capability:
    cap = Capability(
        approval_id=approval.id,
        proposal_id=proposal.id,
        account_id=approval.account_id,
        purpose=proposal.kind,
        canonical_hash=proposal.canonical_hash,
        payload_hash=approval.payload_hash,
        expires_at=expires_at or approval.expires_at,
        status="ACTIVE",
    )
    s.add(cap)
    s.flush()
    return cap


def consume(s: Session, capability_id: str, job_id: str) -> tuple[bool, str, str]:
    """Atomic CAS. Returns (ok, code, human reason)."""
    row = s.execute(
        text(
            "UPDATE capabilities SET used_at = now(), status = 'USED', used_by_job = :job "
            "WHERE id = :id AND used_at IS NULL AND status = 'ACTIVE' AND expires_at > now() RETURNING id"
        ),
        {"id": capability_id, "job": job_id},
    ).first()
    if row:
        return True, "OK", "Capability consumed."
    cap = s.get(Capability, capability_id)
    if cap is None:
        return False, "CAPABILITY_UNKNOWN", "No such capability exists; nothing was executed."
    if cap.used_at is not None:
        return False, "CAPABILITY_REPLAY", f"Capability already used by job {cap.used_by_job}; replay rejected, nothing was sent."
    if cap.expires_at <= datetime.now(UTC):
        if cap.status == "ACTIVE":
            s.execute(text("UPDATE capabilities SET status='EXPIRED' WHERE id=:id AND status='ACTIVE'"), {"id": capability_id})
        return False, "APPROVAL_EXPIRED", "Approval expired because the 60-second approval window elapsed. Nothing was sent."
    return False, "CAPABILITY_REVOKED", f"Capability is {cap.status.lower()}; nothing was sent."
