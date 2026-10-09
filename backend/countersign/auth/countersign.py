"""Approval service. Issues a fresh challenge bound to the exact canonical order/plan/mandate and verifies the
trader's countersignature. On success it mints a single-use capability and queues an execution job.
It never calls the broker: only the isolated executor can."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import generate_authentication_options, options_to_json
from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

from countersign.auth import factors
from countersign.core import audit, capabilities, events, market, state
from countersign.core.approval_crypto import (
    CountersignError,
    b64url_decode,
    challenge_bytes,
    payload_hash,
    server_attest,
    verify_webauthn,
)
from countersign.core.canonical import canonical_hash
from countersign.core.config import settings
from countersign.core.models import Approval, DeviceCredential, ExecJob, Plan, Proposal, StandingMandate, User
from countersign.core.money import inr
from countersign.core.schemas import ApprovalPayload, PriceRef, iso


def _active_credentials(s: Session, user_id: str) -> list[DeviceCredential]:
    return list(
        s.execute(
            select(DeviceCredential).where(DeviceCredential.user_id == user_id, DeviceCredential.revoked_at.is_(None)).order_by(DeviceCredential.created_at.desc())
        ).scalars()
    )


def signing_credentials(payload: dict) -> list[str]:
    """Passkeys a WebAuthn approval may be signed with, as named in the signed payload itself."""
    return [c for c in str(payload.get("credential_ref") or "").split(",") if c]


def _price_refs(p: Proposal) -> list[PriceRef]:
    refs: dict[str, PriceRef] = {}
    band = settings().price_band_bps
    if p.kind == "MANDATE" and not p.steps_view:
        iid = p.canonical["instrument"]["instrument_id"]
        tick = market.latest(iid)
        if tick is None:
            raise CountersignError("STALE_MARKET_DATA", "No live price for this instrument; cannot bind the approval to a market state.")
        refs[iid] = PriceRef(instrument_id=iid, reference_price_paise=tick.ltp_paise, band_low_paise=tick.ltp_paise * (10000 - band) // 10000, band_high_paise=tick.ltp_paise * (10000 + band) // 10000)
    for sv in p.steps_view or []:
        iid = sv["canonical"]["instrument"]["instrument_id"]
        if iid in refs:
            continue
        tick = market.latest(iid)
        if tick is None or tick.stale:
            raise CountersignError("STALE_MARKET_DATA", f"Market data for {sv['canonical']['instrument']['symbol']} is stale. The approval cannot be bound to a reviewed price.")
        refs[iid] = PriceRef(
            instrument_id=iid,
            reference_price_paise=tick.ltp_paise,
            band_low_paise=tick.ltp_paise * (10000 - band) // 10000,
            band_high_paise=tick.ltp_paise * (10000 + band) // 10000,
        )
    return list(refs.values())


def check_band(refs: list[dict]) -> None:
    for r in refs:
        tick = market.latest(r["instrument_id"])
        if tick is None or tick.stale:
            raise CountersignError("STALE_MARKET_DATA", "Market data went stale since you reviewed this order. Review it again.")
        if not (r["band_low_paise"] <= tick.ltp_paise <= r["band_high_paise"]):
            raise CountersignError(
                "PRICE_DRIFT",
                f"Approval invalidated because the market price moved outside the approved price band ({inr(r['band_low_paise'])} to {inr(r['band_high_paise'])}); it is now {inr(tick.ltp_paise)}. Review the order again.",
            )


def start(s: Session, user: User, proposal_id: str, method: str, *, issued_at: datetime | None = None, rp_id: str | None = None) -> dict:
    p = s.get(Proposal, proposal_id, with_for_update=True)
    if p is None or p.user_id != user.id:
        raise CountersignError("NOT_FOUND", "No such proposal.")
    if state.kill_switch(s)["engaged"]:
        raise CountersignError("KILL_SWITCH_ENGAGED", "Execution is halted by the kill switch. Nothing can be approved until it is released.")
    if p.status == "BLOCKED":
        raise CountersignError("PROPOSAL_BLOCKED", "This proposal failed verification and cannot be approved. Revise your request.")
    if p.status != "AWAITING_APPROVAL":
        raise CountersignError("ALREADY_APPROVED", f"This proposal is {p.status.lower().replace('_', ' ')} and cannot be approved again.")
    if canonical_hash(p.canonical) != p.canonical_hash:
        raise CountersignError("PAYLOAD_MISMATCH", "The stored order no longer matches its hash. Refusing to approve a modified order.")

    cred_ref = f"{method}:{user.id}"
    creds = []
    if method == "webauthn":
        creds = _active_credentials(s, user.id)
        if not creds:
            raise CountersignError("NO_DEVICE_REGISTERED", "Register this device's passkey first (Security Center), or use your app PIN.")
        # Every active passkey may sign; the signed payload names exactly which ones (credential ids never hold a comma).
        cred_ref = ",".join(c.credential_id for c in creds)
    elif method == "pin" and not user.pin_hash:
        raise CountersignError("PIN_NOT_SET", "No app PIN is set. Set one in the Security Center.")
    elif method == "totp" and not user.totp_confirmed:
        raise CountersignError("TOTP_NOT_SET", "No authenticator app is set up. Set one up in the Security Center.")
    elif method not in ("webauthn", "pin", "totp"):
        raise CountersignError("METHOD_INVALID", "Unknown approval method.")

    now = (issued_at or datetime.now(UTC)).replace(microsecond=0)
    expires = now + timedelta(seconds=settings().approval_ttl_seconds)
    mandate_id = None
    if p.kind == "MANDATE":
        m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == p.id)).scalar_one()
        mandate_id = m.id
    payload = ApprovalPayload(
        purpose=p.kind,
        proposal_id=p.id,
        canonical_hash=p.canonical_hash,
        account_id=p.account_id,
        user_id=user.id,
        credential_ref=cred_ref,
        price_refs=_price_refs(p),
        nonce=secrets.token_hex(16),
        issued_at=iso(now),
        expires_at=iso(expires),
        mandate_id=mandate_id,
    )
    ph = payload_hash(payload)
    a = Approval(
        proposal_id=p.id,
        user_id=user.id,
        account_id=p.account_id,
        method=method,
        payload=payload.model_dump(mode="json"),
        payload_hash=ph,
        nonce=payload.nonce,
        issued_at=now,
        expires_at=expires,
        # Bound at completion to whichever listed passkey actually signed.
        credential_id=creds[0].credential_id if len(creds) == 1 else None,
        sign_count_before=creds[0].sign_count if len(creds) == 1 else None,
        status="CHALLENGED",
    )
    s.add(a)
    s.flush()
    audit.record(s, "approval", "AUTH_CHALLENGE_ISSUED", "approval", a.id, {"proposal_id": p.id, "method": method, "payload_hash": ph, "expires_at": iso(expires)})
    opts = None
    if method == "webauthn":
        o = generate_authentication_options(
            rp_id=rp_id or settings().rp_id,
            challenge=challenge_bytes(payload),
            allow_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(c.credential_id)) for c in creds],
            user_verification=UserVerificationRequirement.REQUIRED,
            timeout=settings().approval_ttl_seconds * 1000,
        )
        import json

        opts = json.loads(options_to_json(o))
    return {
        "approval_id": a.id,
        "issued_at": iso(now),
        "expires_at": iso(expires),
        "payload": payload.model_dump(mode="json"),
        "payload_hash": ph,
        "webauthn": opts,
    }


def _fail(s: Session, a: Approval, code: str, msg: str, *, severity: str = "warning") -> None:
    a.status = "EXPIRED" if code == "APPROVAL_EXPIRED" else "FAILED"
    a.failure_reason = msg
    audit.record(s, "approval", "SIGNATURE_REJECTED", "approval", a.id, {"code": code, "message": msg})
    if code in ("SIGNATURE_INVALID", "PAYLOAD_MISMATCH"):
        state.security_event(s, "critical" if code == "PAYLOAD_MISMATCH" else "warning", code, msg, {"approval_id": a.id, "proposal_id": a.proposal_id}, actor="approval")


def complete(
    s: Session,
    user: User,
    proposal_id: str,
    approval_id: str,
    *,
    webauthn: dict | None = None,
    pin: str | None = None,
    totp: str | None = None,
    source: str = "approval",
    hold_job: bool = False,
) -> Proposal:
    a = s.get(Approval, approval_id, with_for_update=True)
    p = s.get(Proposal, proposal_id, with_for_update=True)
    if a is None or p is None or a.proposal_id != p.id or a.user_id != user.id:
        raise CountersignError("NOT_FOUND", "No such approval for this proposal.")
    if a.status != "CHALLENGED":
        raise CountersignError("ALREADY_APPROVED", f"This approval is already {a.status.lower()}.")
    try:
        if state.kill_switch(s)["engaged"]:
            raise CountersignError("KILL_SWITCH_ENGAGED", "Execution is halted by the kill switch.")
        if datetime.now(UTC) >= a.expires_at:
            raise CountersignError("APPROVAL_EXPIRED", "Approval expired because the 60-second approval window elapsed. Review the order again.")
        if payload_hash(a.payload) != a.payload_hash:
            raise CountersignError("PAYLOAD_MISMATCH", "The stored approval payload was altered after the challenge was issued.")
        if canonical_hash(p.canonical) != a.payload["canonical_hash"] or p.canonical_hash != a.payload["canonical_hash"]:
            raise CountersignError("PAYLOAD_MISMATCH", "The order changed after you reviewed it. Your signature covers the reviewed order only; review again.")
        if p.status != "AWAITING_APPROVAL":
            raise CountersignError("ALREADY_APPROVED", "This proposal can no longer be approved.")
        if p.kind != "MANDATE":
            check_band(a.payload["price_refs"])

        if a.method == "webauthn":
            if not webauthn:
                raise CountersignError("SIGNATURE_INVALID", "No passkey response was provided.")
            used = webauthn.get("id") or webauthn.get("rawId")
            if not isinstance(used, str) or used not in signing_credentials(a.payload):
                raise CountersignError("SIGNATURE_INVALID", "The passkey used is not one this approval was issued for.")
            cred = s.execute(select(DeviceCredential).where(DeviceCredential.credential_id == used)).scalar_one_or_none()
            if cred is None or cred.revoked_at is not None or cred.user_id != user.id:
                raise CountersignError("SIGNATURE_INVALID", "This device is not registered (or was revoked) for your account.")
            a.credential_id = cred.credential_id
            a.sign_count_before = cred.sign_count
            new_count = verify_webauthn(webauthn, a.payload, credential_public_key=cred.public_key, sign_count_before=a.sign_count_before or 0, expected_credential_id=cred.credential_id)
            cred.sign_count = new_count
            cred.last_used_at = datetime.now(UTC)
            a.assertion = webauthn
        elif a.method == "pin":
            factors.verify_pin(s, user, pin or "")
            a.server_signature = server_attest(a.payload_hash, "pin")
        else:
            factors.verify_totp(user, totp or "")
            a.server_signature = server_attest(a.payload_hash, "totp")
    except CountersignError as e:
        if e.code not in ("ALREADY_APPROVED",):
            if e.code in ("PIN_INVALID", "PIN_LOCKED", "TOTP_INVALID", "TOTP_REPLAY"):
                audit.record(s, "approval", "SIGNATURE_REJECTED", "approval", a.id, {"code": e.code})
                s.commit()
            else:
                _fail(s, a, e.code, e.message)
                s.commit()
        raise

    now = datetime.now(UTC)
    a.status = "VERIFIED"
    a.verified_at = now
    audit.record(s, "approval", "SIGNATURE_VERIFIED", "approval", a.id, {"method": a.method, "payload_hash": a.payload_hash, "proposal_id": p.id})
    if p.kind == "MANDATE":
        m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == p.id)).scalar_one()
        cap = capabilities.mint(s, a, p, expires_at=m.expires_at)
        m.state = "ACTIVE"
        m.approval_id = a.id
        m.capability_id = cap.id
        m.activated_at = now
        p.status = "ACTIVE"
        audit.record(s, "approval", "CAPABILITY_MINTED", "capability", cap.id, {"purpose": "MANDATE", "mandate_id": m.id, "expires_at": iso(m.expires_at), "single_use": True})
        audit.record(s, "approval", "MANDATE_ARMED", "mandate", m.id, {"proposal_id": p.id})
        state.outcome(s, "MANDATE_ARMED", "Standing instruction armed", p.summary, "info", proposal_id=p.id, account_id=p.account_id)
    else:
        cap = capabilities.mint(s, a, p)
        p.status = "APPROVED"
        job = ExecJob(capability_id=cap.id, kind=p.kind, proposal_id=p.id, status="HELD" if hold_job else "PENDING", source=source)
        s.add(job)
        if p.kind == "PLAN":
            plan = s.get(Plan, p.id)
            plan.state = "APPROVED"
            plan.approved_at = now
            audit.record(s, "approval", "PLAN_STATE", "plan", p.id, {"to": "APPROVED"})
        s.flush()
        audit.record(s, "approval", "CAPABILITY_MINTED", "capability", cap.id, {"purpose": p.kind, "expires_at": iso(cap.expires_at), "single_use": True})
        audit.record(s, "approval", "EXECUTION_REQUESTED", "job", job.id, {"capability_id": cap.id, "proposal_id": p.id})
        state.outcome(s, "APPROVAL_VERIFIED", "Countersignature verified", f"{p.summary} (single-use capability minted, expires in 60 s)", "success", proposal_id=p.id, account_id=p.account_id)
    s.flush()
    events.emit_proposal(p)
    events.publish(events.CH_EXEC_WAKE, {"proposal_id": p.id})
    return p
