"""Executor pipeline. Runs ONLY in the executor process, which alone holds the broker write key and never imports
any LLM code. Every check is repeated here from first principles; nothing upstream is trusted.

Broker policy:
- Paper accounts: an ambiguous outcome is reconciled against the broker by tag, then at most one safe retry with the
  same tag when the broker confirms the order is absent (the paper broker never deduplicates tags).
- 021 accounts (accounts.broker_kind = '021'): a placement, modification or cancellation is NEVER retried
  automatically. The 021 gateway reconciles an ambiguous placement itself; the executor records its verdict:
  accepted, rejected, NOT_PLACED (nothing was sent twice; the trader countersigns again to resend) or NEEDS_REVIEW
  (several matching orders, or the outcome could not be confirmed; no kill switch, surfaced to the trader).
- Every broker: a 5xx / 429 / 504 is never treated as a definite rejection without reconciling; a MODIFY or CANCEL is
  confirmed on the target order's real state (never by looking up the modification's own tag); and after a crash an
  order is never resent when it belongs to a previous IST trading day or its approval has expired."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime

from sqlalchemy import select

from countersign.core import audit, capabilities, events, market, state
from countersign.core.approval_crypto import CountersignError, payload_hash, verify_server_attestation, verify_webauthn
from countersign.core.broker_client import BrokerError, BrokerTimeout, BrokerUnavailable, OrderExecutionProvider, broker_kind
from countersign.core.canonical import canonical_hash
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import (
    Account,
    AccountMember,
    Approval,
    Capability,
    DeviceCredential,
    ExecJob,
    OutboxEntry,
    Plan,
    PlanStep,
    Proposal,
    StandingMandate,
)
from countersign.core.money import inr
from countersign.core.schemas import CanonicalMandate, CanonicalOrder, CanonicalPlan

_brokers: dict[str, OrderExecutionProvider] = {}
# Jobs whose preflight ran in THIS process. A step of any other job is being re-run by crash recovery (DBOS replays
# the checkpointed preflight result without calling it), which is when the resend guards below apply.
_live_jobs: set[str] = set()
OPEN_STATUSES = ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED")
FINAL_OUTBOX = ("ACKED", "RECONCILED_FOUND", "REJECTED_BY_BROKER", "FAILED", "NOT_PLACED", "NEEDS_REVIEW")
IST_OFFSET_S = 19800


def broker(account_id: str) -> OrderExecutionProvider:
    if account_id not in _brokers:
        _brokers[account_id] = OrderExecutionProvider(account_id=account_id)
    return _brokers[account_id]


def client_order_id(capability_id: str, step_no: int) -> str:
    return "CS" + hashlib.sha256(f"{capability_id}:{step_no}".encode()).hexdigest()[:14].upper()


class Blocked(Exception):
    def __init__(self, code: str, message: str, security: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.security = security


SECURITY_CODES = {"TAMPERED_ORDER", "PAYLOAD_MISMATCH", "SIGNATURE_INVALID", "CAPABILITY_REPLAY", "MANDATE_SCOPE_VIOLATION", "PLAN_MODIFIED", "CAPABILITY_UNKNOWN"}


def _block_job(s, job: ExecJob, p: Proposal | None, code: str, message: str) -> None:
    job.status = "HALTED" if code == "KILL_SWITCH_ENGAGED" else "REJECTED"
    job.reason = message
    job.reason_code = code
    job.finished_at = datetime.now(UTC)
    if p is not None and p.status in ("APPROVED", "EXECUTING"):
        p.status = "EXPIRED" if code in ("APPROVAL_EXPIRED",) else "FAILED"
        if p.kind == "PLAN":
            plan = s.get(Plan, p.id)
            if plan:
                plan.state = "REJECTED"
            for st in s.execute(select(PlanStep).where(PlanStep.plan_id == p.id)).scalars():
                if st.state == "PENDING":
                    st.state = "SKIPPED"
                    st.reason = message
    if job.mandate_id:
        m = s.get(StandingMandate, job.mandate_id)
        if m and m.state in ("TRIGGERED", "EXECUTING"):
            m.state = "FAILED"
            m.reason = message
    audit.record(s, "executor", "EXECUTION_BLOCKED", "job", job.id, {"code": code, "message": message, "proposal_id": job.proposal_id})
    state.outcome(s, "EXECUTION_BLOCKED", "Execution blocked", message, "danger" if code in SECURITY_CODES else "warning", proposal_id=job.proposal_id, account_id=p.account_id if p else None)
    if code in SECURITY_CODES:
        state.security_event(s, "critical", code, message, {"job_id": job.id, "proposal_id": job.proposal_id}, actor="executor")
    if p is not None:
        events.emit_proposal(p)


def _verify_signature(s, a: Approval) -> None:
    if a.status != "VERIFIED":
        raise Blocked("SIGNATURE_INVALID", "The approval was never verified; execution refused.", True)
    if payload_hash(a.payload) != a.payload_hash:
        raise Blocked("PAYLOAD_MISMATCH", "The approval payload was modified after signing (hash mismatch). Execution refused.", True)
    try:
        if a.method == "webauthn":
            cred = s.execute(select(DeviceCredential).where(DeviceCredential.credential_id == a.credential_id)).scalar_one_or_none()
            if cred is None or cred.revoked_at is not None:
                raise Blocked("SIGNATURE_INVALID", "The signing device is unknown or revoked.", True)
            if cred.user_id != a.user_id or cred.credential_id not in str(a.payload.get("credential_ref") or "").split(","):
                raise Blocked("SIGNATURE_INVALID", "The signing device is not one the signed approval names for this trader.", True)
            verify_webauthn(a.assertion or {}, a.payload, credential_public_key=cred.public_key, sign_count_before=a.sign_count_before or 0, expected_credential_id=cred.credential_id)
        else:
            verify_server_attestation(a.payload_hash, a.method, a.server_signature or "")
    except CountersignError as e:
        raise Blocked(e.code if e.code in SECURITY_CODES else "SIGNATURE_INVALID", f"Execution blocked because authentication could not be verified: {e.message}", True) from e


def _check_band(a: Approval, instrument_id: str) -> None:
    ref = next((r for r in a.payload["price_refs"] if r["instrument_id"] == instrument_id), None)
    if ref is None:
        raise Blocked("PAYLOAD_MISMATCH", "The approval does not cover this instrument.", True)
    tick = market.latest(instrument_id)
    if tick is None or tick.stale:
        raise Blocked("STALE_MARKET_DATA", "Execution blocked: market data is stale, so the approved price band cannot be confirmed.")
    if not (ref["band_low_paise"] <= tick.ltp_paise <= ref["band_high_paise"]):
        raise Blocked(
            "PRICE_DRIFT",
            f"Approval invalidated because the market price moved outside the approved band ({inr(ref['band_low_paise'])} to {inr(ref['band_high_paise'])}); it is now {inr(tick.ltp_paise)}. Nothing was sent. Review and approve again.",
        )


def _mandate_scope(m: StandingMandate, mandate: CanonicalMandate, order: CanonicalOrder) -> None:
    t = mandate.template
    if t is None or m.kind != "ORDER":
        raise Blocked("MANDATE_SCOPE_VIOLATION", "Alert mandates can never place orders.", True)
    if order.instrument.instrument_id != t.instrument.instrument_id or order.side != t.side:
        raise Blocked("MANDATE_SCOPE_VIOLATION", "The fired order does not match the signed mandate's instrument or side.", True)
    if order.quantity > (mandate.max_quantity or 0) or order.quantity != t.quantity:
        raise Blocked("MANDATE_SCOPE_VIOLATION", f"Fired quantity {order.quantity} exceeds the signed mandate scope ({mandate.max_quantity}).", True)
    lim = order.limit_price_paise or 0
    if (t.side.value == "BUY" and lim > (mandate.price_limit_paise or 0)) or (t.side.value == "SELL" and lim < (mandate.price_limit_paise or 0)):
        raise Blocked("MANDATE_SCOPE_VIOLATION", "Fired price is outside the signed mandate's price limit.", True)
    if canonical_hash(order) != canonical_hash(t):
        raise Blocked("MANDATE_SCOPE_VIOLATION", "The fired order differs from the signed order template.", True)


def preflight(job_id: str) -> dict:
    """Claim, verify from first principles, and atomically consume the capability. Returns the steps to send."""
    with session_scope() as s:
        job = s.get(ExecJob, job_id, with_for_update=True)
        if job is None:
            return {"ok": False, "code": "UNKNOWN_JOB"}
        cap = s.get(Capability, job.capability_id)
        p = s.get(Proposal, job.proposal_id)
        if job.status == "RUNNING" and cap and cap.used_by_job == job.id:
            return {"ok": True, "steps": _steps_for(s, job, p), "recovered": True}
        if job.status != "PENDING":
            return {"ok": False, "code": "NOT_PENDING", "status": job.status}
        job.started_at = datetime.now(UTC)
        try:
            if state.kill_switch(s)["engaged"]:
                raise Blocked("KILL_SWITCH_ENGAGED", "Execution halted: the kill switch is engaged. Nothing was sent.")
            if cap is None:
                raise Blocked("CAPABILITY_UNKNOWN", "No capability backs this execution request.", True)
            a = s.get(Approval, cap.approval_id)
            if p is None or a is None:
                raise Blocked("CAPABILITY_UNKNOWN", "Capability is not bound to a valid approval.", True)
            if cap.account_id != p.account_id or a.account_id != p.account_id or a.payload["account_id"] != p.account_id:
                raise Blocked("PAYLOAD_MISMATCH", "Capability, approval and order belong to different accounts.", True)
            owner = s.get(Account, p.account_id)
            operates = owner is not None and (owner.user_id == a.user_id or s.get(AccountMember, (p.account_id, a.user_id)) is not None)
            if operates and owner.broker_kind == "021":
                # A 021 account is operated only by a trader who still holds a self-service link to it, or (the
                # operator-linked .env account) whose email is still listed in CS_021_OWNER_EMAILS.
                from countersign.broker021.linking import may_operate_021
                from countersign.core.models import User

                operates = may_operate_021(s, owner, s.get(User, a.user_id))
            if not operates or p.user_id != a.user_id:
                raise Blocked("PAYLOAD_MISMATCH", "The approving trader does not own the account this order is for. Execution refused.", True)
            if canonical_hash(p.canonical) != cap.canonical_hash or cap.canonical_hash != a.payload["canonical_hash"] or p.canonical_hash != cap.canonical_hash:
                raise Blocked("TAMPERED_ORDER", "The order was modified after approval: its canonical hash no longer matches the signed hash. Execution refused.", True)
            if cap.payload_hash != a.payload_hash:
                raise Blocked("PAYLOAD_MISMATCH", "Capability is not bound to this approval payload.", True)
            _verify_signature(s, a)
            if job.kind != "MANDATE" and datetime.now(UTC) >= a.expires_at:
                raise Blocked("APPROVAL_EXPIRED", "Approval expired because the 60-second approval window elapsed. Nothing was sent.")
            if job.kind == "MANDATE":
                m = s.get(StandingMandate, job.mandate_id)
                if m is None or m.state != "TRIGGERED":
                    raise Blocked("MANDATE_SCOPE_VIOLATION", "Mandate is not in a triggered state; refusing to fire.", True)
                mandate = CanonicalMandate.model_validate(p.canonical)
                order = CanonicalOrder.model_validate(job.order_request)
                _mandate_scope(m, mandate, order)
                if datetime.now(UTC) >= m.expires_at:
                    raise Blocked("APPROVAL_EXPIRED", "The standing instruction expired before it could fire.")
            if job.kind == "PLAN":
                plan_row = s.get(Plan, p.id)
                if plan_row is None or plan_row.plan_hash != canonical_hash(p.canonical):
                    raise Blocked("PLAN_MODIFIED", "Plan was not executed because the signed version is immutable and the stored plan differs from it.", True)
            ok, code, reason = capabilities.consume(s, cap.id, job.id)
            if not ok:
                raise Blocked(code, reason, code in SECURITY_CODES)
        except Blocked as b:
            _block_job(s, job, p, b.code, b.message)
            return {"ok": False, "code": b.code, "message": b.message}
        job.status = "RUNNING"
        p.status = "EXECUTING"
        if job.kind == "MANDATE":
            m = s.get(StandingMandate, job.mandate_id)
            m.state = "EXECUTING"
        if job.kind == "PLAN":
            s.get(Plan, p.id).state = "EXECUTING"
            audit.record(s, "executor", "PLAN_STATE", "plan", p.id, {"to": "EXECUTING"})
        audit.record(s, "executor", "EXECUTION_STARTED", "job", job.id, {"capability_id": cap.id, "proposal_id": p.id, "verified": ["canonical_hash", "payload_hash", "signature", "expiry", "account_binding", "single_use"]})
        events.emit_proposal(p)
        _live_jobs.add(job.id)
        return {"ok": True, "steps": _steps_for(s, job, p)}


def _steps_for(s, job: ExecJob, p: Proposal) -> list[dict]:
    if job.kind == "PLAN":
        plan = CanonicalPlan.model_validate(p.canonical)
        return [{"step_no": st.step_no, "order": st.order.model_dump(mode="json"), "depends_on": st.depends_on} for st in plan.steps]
    if job.kind == "MANDATE":
        return [{"step_no": 1, "order": job.order_request, "depends_on": None}]
    return [{"step_no": 1, "order": p.canonical, "depends_on": None}]


def _request_body(o: CanonicalOrder, tag: str) -> dict:
    return {
        "instrument_id": o.instrument.instrument_id,
        "symbol": o.instrument.symbol,
        "segment": o.instrument.segment,
        "side": o.side.value,
        "quantity": o.quantity,
        "order_type": o.order_type.value,
        "product": o.product.value,
        "validity": o.validity.value,
        "limit_price_paise": o.limit_price_paise,
        "trigger_price_paise": o.trigger_price_paise,
        "protection_price_paise": o.protection_price_paise,
        "tag": tag,
    }


def _set_step(s, proposal_id: str, step_no: int, **kw) -> None:
    st = s.get(PlanStep, (proposal_id, step_no))
    if st:
        for k, v in kw.items():
            setattr(st, k, v)


def send_step(job_id: str, step: dict) -> dict:
    """Write-ahead outbox, single send, reconcile on ambiguity. Idempotent across crashes and DBOS recovery."""
    step_no = step["step_no"]
    order = CanonicalOrder.model_validate(step["order"])
    with session_scope() as s:
        job = s.get(ExecJob, job_id)
        p = s.get(Proposal, job.proposal_id)
        a = s.get(Approval, s.get(Capability, job.capability_id).approval_id)
        tag = client_order_id(job.capability_id, step_no)
        ob = s.execute(select(OutboxEntry).where(OutboxEntry.capability_id == job.capability_id, OutboxEntry.step_no == step_no)).scalar_one_or_none()
        if ob is not None and ob.state in FINAL_OUTBOX:
            return {"ok": ob.state in ("ACKED", "RECONCILED_FOUND"), "outbox_id": ob.id, "broker_order_id": ob.broker_order_id, "state": ob.state}
        recovered = job.id not in _live_jobs
        stale = _resend_block_reason(s, job, a) if recovered else None
        if stale and (ob is None or ob.state == "PREPARED"):
            # Crash recovery found a step that was never sent, but its approval has expired or it belongs to a
            # previous trading day: it is never sent now.
            if ob is not None:
                ob.state = "FAILED"
                ob.reject_reason = stale
            if job.kind == "PLAN":
                _set_step(s, p.id, step_no, state="FAILED", reason=stale)
            audit.record(s, "executor", "RECOVERY_NOT_SENT", "job", job.id, {"step_no": step_no, "reason": stale})
            state.outcome(s, "EXECUTION_BLOCKED", "Not sent after restart", stale, "warning", proposal_id=p.id, account_id=p.account_id)
            if job.kind != "PLAN":
                _block_job(s, job, p, "APPROVAL_EXPIRED", stale)
            return {"ok": False, "code": "APPROVAL_EXPIRED", "message": stale}
        if ob is None:
            try:
                if state.kill_switch(s)["engaged"]:
                    raise Blocked("KILL_SWITCH_ENGAGED", "Execution halted by the kill switch before this step was sent.")
                if order.action == "PLACE" and job.kind != "MANDATE":
                    _check_band(a, order.instrument.instrument_id)
                if job.kind == "MANDATE":
                    tick = market.latest(order.instrument.instrument_id)
                    if tick is None or tick.stale:
                        raise Blocked("STALE_MARKET_DATA", "Execution blocked: market data is stale at trigger time.")
                if job.kind == "PLAN":
                    st = s.get(PlanStep, (p.id, step_no))
                    if st is None or st.step_hash != canonical_hash(order):
                        raise Blocked("PLAN_MODIFIED", "Plan step differs from the signed plan; refusing to execute it.", True)
            except Blocked as b:
                if job.kind == "PLAN":
                    _set_step(s, p.id, step_no, state="FAILED", reason=b.message)
                if b.code == "KILL_SWITCH_ENGAGED" or job.kind != "PLAN":
                    _block_job(s, job, p, b.code, b.message)
                else:
                    audit.record(s, "executor", "EXECUTION_BLOCKED", "plan_step", f"{p.id}:{step_no}", {"code": b.code, "message": b.message})
                    state.outcome(s, "EXECUTION_BLOCKED", f"Plan step {step_no} not sent", b.message, "warning", proposal_id=p.id, account_id=p.account_id)
                    if b.security:
                        state.security_event(s, "critical", b.code, b.message, {"job_id": job.id}, actor="executor")
                return {"ok": False, "code": b.code, "message": b.message}
            ob = OutboxEntry(
                job_id=job.id,
                proposal_id=p.id,
                capability_id=job.capability_id,
                step_no=step_no,
                client_order_id=tag,
                idempotency_key=f"{job.capability_id}:{step_no}",
                correlation_id=job.id,
                request=_request_body(order, tag) | {"action": order.action, "target_broker_order_id": order.target_broker_order_id},
                request_hash=canonical_hash(order),
                state="PREPARED",
            )
            s.add(ob)
            s.flush()
            audit.record(s, "executor", "OUTBOX_PREPARED", "outbox", ob.id, {"client_order_id": tag, "idempotency_key": ob.idempotency_key, "step_no": step_no})
        outbox_id = ob.id
        prior_state = ob.state
        acct_kind = broker_kind(p.account_id)
    if prior_state in ("SENDING", "UNKNOWN", "RECONCILED_ABSENT"):
        # A previous run may have sent it: reconcile, never blindly resend.
        if acct_kind == "021":
            return _resolve_021(outbox_id, order) if order.action == "PLACE" else _confirm_target(outbox_id, order, sent_ok=False, allow_retry=False)
        return reconcile(outbox_id, allow_retry=not stale)
    if acct_kind == "021":
        return _send_021(outbox_id, order)
    return _send(outbox_id, order)


def _ist_day(ts: datetime | float) -> str:
    secs = ts.timestamp() if isinstance(ts, datetime) else float(ts)
    return datetime.fromtimestamp(secs + IST_OFFSET_S, UTC).date().isoformat()


def _resend_block_reason(s, job: ExecJob, a: Approval) -> str | None:
    """Why a recovered job must not send (or resend) anything now, or None when it still may."""
    now = datetime.now(UTC)
    started = job.started_at or job.created_at or now
    if _ist_day(started) != _ist_day(now):
        return f"Not sent after a restart: this order was approved on {_ist_day(started)} (IST), a previous trading day. Nothing was sent; countersign again if you still want it."
    if job.kind == "MANDATE":
        m = s.get(StandingMandate, job.mandate_id) if job.mandate_id else None
        if m is None or now >= m.expires_at:
            return "Not sent after a restart: the standing instruction expired. Nothing was sent."
        return None
    if now >= a.expires_at:
        return "Not sent after a restart: the 60-second approval had expired by the time the executor recovered. Nothing was sent; countersign again if you still want it."
    return None


def _send(outbox_id: str, order: CanonicalOrder) -> dict:
    acct = order.account_id
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        ob.state = "SENDING"
        ob.attempts += 1
        audit.record(s, "executor", "BROKER_REQUEST", "outbox", ob.id, {"client_order_id": ob.client_order_id, "attempt": ob.attempts, "action": order.action})
        if ob.proposal_id:
            _set_step(s, ob.proposal_id, ob.step_no, state="SUBMITTED", client_order_id=ob.client_order_id)
        body = dict(ob.request)
    if order.action != "PLACE":
        return _modify_or_cancel(outbox_id, order, allow_retry=True)
    try:
        body.pop("action", None)
        body.pop("target_broker_order_id", None)
        resp = broker(acct).place(body)
    except BrokerTimeout:
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            ob.state = "UNKNOWN"
            audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "TIMEOUT", "note": "Outcome unknown; reconciling against the broker order book before any retry."})
            state.outcome(s, "RECONCILED", "Broker response lost", f"Order {ob.client_order_id}: no response from the broker. Reconciling against the order book instead of resending.", "warning", proposal_id=ob.proposal_id)
        return reconcile(outbox_id, allow_retry=True)
    except (BrokerUnavailable, BrokerError) as e:
        # A 5xx, 429 or 504 says nothing definite about the order: it is reconciled like a timeout, never treated as
        # a rejection.
        not_accepted = isinstance(e, BrokerUnavailable) or (isinstance(e, BrokerError) and (e.code == "NOT_ACCEPTED" or _ambiguous_http(e)))
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            if not_accepted:
                ob.state = "UNKNOWN"
                audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "NOT_ACCEPTED" if not isinstance(e, BrokerError) or e.code == "NOT_ACCEPTED" else f"HTTP_{e.status}", "detail": str(e)})
            else:
                ob.state = "REJECTED_BY_BROKER"
                ob.reject_reason = e.message
                audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "ERROR", "code": e.code, "message": e.message})
                _set_step(s, ob.proposal_id, ob.step_no, state="REJECTED", reason=e.message)
                state.outcome(s, "ORDER_REJECTED", "Broker refused the order", e.message, "danger", proposal_id=ob.proposal_id)
        if not_accepted:
            definite = isinstance(e, BrokerUnavailable) or e.code == "NOT_ACCEPTED"
            return reconcile(outbox_id, allow_retry=True, window_s=1.5 if definite else None)
        return {"ok": False, "outbox_id": outbox_id, "state": "REJECTED_BY_BROKER"}
    return _record_ack(outbox_id, resp, reconciled=False)


def _ambiguous_http(e: BrokerError) -> bool:
    return e.status >= 500 or e.status in (408, 429)


# ---------------------------------------------------------------- MODIFY / CANCEL: confirmed on the target order


def _modify_body(order: CanonicalOrder) -> dict:
    """The signed new state (CanonicalOrder for MODIFY carries the full new order: quantity, limit and trigger)."""
    limit = order.limit_price_paise if order.limit_price_paise is not None else order.protection_price_paise
    body: dict = {"quantity": order.quantity, "limit_price_paise": limit}
    if order.trigger_price_paise is not None:
        body["trigger_price_paise"] = order.trigger_price_paise
    return body


def _target_state(acct: str, order: CanonicalOrder) -> tuple[bool | None, dict | None]:
    """True when the target order shows the signed change, False when it can no longer get it (it completed or was
    cancelled / rejected another way), None while still undecided or unreadable."""
    try:
        o = broker(acct).order(order.target_broker_order_id)
    except (BrokerTimeout, BrokerUnavailable, BrokerError):
        return None, None
    if order.action == "CANCEL":
        if o.get("status") == "CANCELLED":
            return True, o
        if o.get("status") in ("COMPLETE", "REJECTED"):
            return False, o
        return None, o
    want = _modify_body(order)
    applied = (o.get("limit_price_paise") or o.get("protection_price_paise") or None) == (want["limit_price_paise"] or None) and int(o.get("quantity") or 0) == int(want["quantity"])
    if "trigger_price_paise" in want:
        applied = applied and (o.get("trigger_price_paise") or None) == (want["trigger_price_paise"] or None)
    if applied:
        return True, o
    if o.get("status") not in OPEN_STATUSES:
        return False, o
    return None, o


def _modify_or_cancel(outbox_id: str, order: CanonicalOrder, *, allow_retry: bool) -> dict:
    acct = order.account_id
    sent_ok = False
    try:
        if order.action == "MODIFY":
            broker(acct).modify(order.target_broker_order_id, _modify_body(order))
        else:
            broker(acct).cancel(order.target_broker_order_id)
        sent_ok = True
    except BrokerTimeout:
        pass
    except BrokerUnavailable as e:
        if broker_kind(acct) == "021":
            return _not_placed(outbox_id, f"The 021 gateway was unreachable ({e}); the {order.action.lower()} was not sent.")
    except BrokerError as e:
        if not _ambiguous_http(e) or e.code in ("MODIFY_NOT_SENT", "CANCEL_NOT_SENT"):
            if e.code in ("MODIFY_NOT_SENT", "CANCEL_NOT_SENT"):
                return _not_placed(outbox_id, e.message)
            return _refused(outbox_id, e)
    return _confirm_target(outbox_id, order, sent_ok=sent_ok, allow_retry=allow_retry and broker_kind(acct) != "021")


def _confirm_target(outbox_id: str, order: CanonicalOrder, *, sent_ok: bool, allow_retry: bool) -> dict:
    acct = order.account_id
    deadline = time.time() + settings().reconcile_window_s
    verdict, o = _target_state(acct, order)
    while verdict is None and time.time() < deadline:
        time.sleep(0.5)
        verdict, o = _target_state(acct, order)
    what = "modification" if order.action == "MODIFY" else "cancellation"
    if verdict is True:
        return _record_ack(outbox_id, o, reconciled=not sent_ok)
    if verdict is False:
        reason = f"Order {order.target_broker_order_id} is {o.get('status')} at the broker, so the {what} did not apply."
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            ob.state = "REJECTED_BY_BROKER"
            ob.broker_order_id = order.target_broker_order_id
            ob.broker_status = o.get("status")
            ob.reject_reason = reason
            audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "TARGET_NOT_OPEN", "status": o.get("status")})
            _set_step(s, ob.proposal_id, ob.step_no, state="REJECTED", reason=reason)
            state.outcome(s, "ORDER_REJECTED", f"{what.capitalize()} not applied", reason, "warning", proposal_id=ob.proposal_id, broker_order_id=order.target_broker_order_id)
        return {"ok": False, "outbox_id": outbox_id, "state": "REJECTED_BY_BROKER"}
    if sent_ok and o is not None:
        # The broker accepted the request but the order does not show it yet (021 applies them asynchronously).
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            audit.record(s, "executor", "RECONCILIATION", "outbox", ob.id, {"result": "ACCEPTED_NOT_YET_VISIBLE", "target": order.target_broker_order_id})
            state.outcome(s, "RECONCILED", f"{what.capitalize()} accepted, not yet visible", f"The broker accepted the {what} of order {order.target_broker_order_id}; the order does not show it yet. Live updates will report it.", "info", proposal_id=ob.proposal_id, broker_order_id=order.target_broker_order_id)
        return _record_ack(outbox_id, o, reconciled=False)
    if allow_retry:
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            if ob.attempts < 2:
                ob.state = "RECONCILED_ABSENT"
                audit.record(s, "executor", "RETRY", "outbox", ob.id, {"reason": f"The target order does not show the {what}; one safe retry (it is idempotent on the paper broker)."})
                retry = True
            else:
                retry = False
        if retry:
            with session_scope() as s:
                ob = s.get(OutboxEntry, outbox_id)
                ob.state = "SENDING"
                ob.attempts += 1
            return _modify_or_cancel(outbox_id, order, allow_retry=False)
    if broker_kind(acct) == "021":
        return _not_placed(outbox_id, f"The {what} was not confirmed and order {order.target_broker_order_id} still shows its previous state.")
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        ob.state = "FAILED"
        ob.reject_reason = f"The {what} could not be confirmed on order {order.target_broker_order_id}; not retried again. Check the order before trying again."
        _set_step(s, ob.proposal_id, ob.step_no, state="FAILED", reason=ob.reject_reason)
        state.outcome(s, "EXECUTION_BLOCKED", "Outcome unknown", ob.reject_reason, "danger", proposal_id=ob.proposal_id)
    return {"ok": False, "outbox_id": outbox_id, "state": "FAILED"}


def _refused(outbox_id: str, e: BrokerError) -> dict:
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        ob.state = "REJECTED_BY_BROKER"
        ob.reject_reason = e.message
        audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "ERROR", "code": e.code, "message": e.message, "http_status": e.status})
        _set_step(s, ob.proposal_id, ob.step_no, state="REJECTED", reason=e.message)
        state.outcome(s, "ORDER_REJECTED", "Broker refused the order", e.message, "danger", proposal_id=ob.proposal_id)
    return {"ok": False, "outbox_id": outbox_id, "state": "REJECTED_BY_BROKER", "code": e.code}


# ---------------------------------------------------------------- 021: sent once, never retried


def _not_placed(outbox_id: str, message: str, data: dict | None = None) -> dict:
    reason = f"{message} Nothing was sent twice; countersign again to resend."
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        ob.state = "NOT_PLACED"
        ob.reject_reason = reason
        audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "NOT_PLACED", "detail": message, **(data or {})})
        _set_step(s, ob.proposal_id, ob.step_no, state="NOT_PLACED", reason=reason)
        state.outcome(s, "ORDER_NOT_PLACED", "Order not placed", reason, "warning", proposal_id=ob.proposal_id)
    return {"ok": False, "outbox_id": outbox_id, "state": "NOT_PLACED", "message": reason}


def _needs_review(outbox_id: str, message: str, data: dict | None = None) -> dict:
    reason = f"{message} Countersign did not retry and did not guess. Review the 021 order book before placing it again."
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        ob.state = "NEEDS_REVIEW"
        ob.reject_reason = reason
        audit.record(s, "executor", "RECONCILIATION", "outbox", ob.id, {"result": "NEEDS_REVIEW", "detail": message, **(data or {})})
        _set_step(s, ob.proposal_id, ob.step_no, state="NEEDS_REVIEW", reason=reason)
        state.outcome(s, "ORDER_NEEDS_REVIEW", "Order outcome needs review", reason, "danger", proposal_id=ob.proposal_id)
    return {"ok": False, "outbox_id": outbox_id, "state": "NEEDS_REVIEW", "message": reason}


NOT_PLACED_CODES = {"NOT_PLACED", "DUPLICATE_IN_FLIGHT", "INSTRUMENT_NOT_ON_021", "UNMAPPABLE_ORDER", "TAG_REQUIRED", "TAG_ALREADY_USED", "ACCOUNT_FORBIDDEN", "SCOPE_FORBIDDEN", "UNAUTHENTICATED", "ACCOUNT_REQUIRED"}
REVIEW_CODES = {"AMBIGUOUS", "UNCONFIRMED"}


def _send_021(outbox_id: str, order: CanonicalOrder) -> dict:
    acct = order.account_id
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        ob.state = "SENDING"
        ob.attempts += 1
        audit.record(s, "executor", "BROKER_REQUEST", "outbox", ob.id, {"client_order_id": ob.client_order_id, "attempt": ob.attempts, "action": order.action, "broker": "021"})
        if ob.proposal_id:
            _set_step(s, ob.proposal_id, ob.step_no, state="SUBMITTED", client_order_id=ob.client_order_id)
        body = dict(ob.request)
    if order.action != "PLACE":
        return _modify_or_cancel(outbox_id, order, allow_retry=False)
    body.pop("action", None)
    body.pop("target_broker_order_id", None)
    try:
        resp = broker(acct).place(body)
    except BrokerUnavailable as e:
        return _not_placed(outbox_id, f"The 021 gateway was unreachable ({e}), so nothing reached 021.")
    except BrokerTimeout:
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            ob.state = "UNKNOWN"
            audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "TIMEOUT", "note": "Waiting for the gateway's reconciliation verdict; never resent."})
        return _resolve_021(outbox_id, order)
    except BrokerError as e:
        if e.code in NOT_PLACED_CODES:
            return _not_placed(outbox_id, e.message, {"code": e.code, "broker_error": e.body.get("broker_error")})
        if e.code in REVIEW_CODES:
            return _needs_review(outbox_id, e.message, {"code": e.code, "candidates": e.body.get("candidates")})
        if _ambiguous_http(e):
            with session_scope() as s:
                ob = s.get(OutboxEntry, outbox_id)
                ob.state = "UNKNOWN"
                audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": f"HTTP_{e.status}", "detail": e.message})
            return _resolve_021(outbox_id, order)
        return _refused(outbox_id, e)
    if resp.get("reconciled"):
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            audit.record(s, "executor", "RECONCILIATION", "outbox", ob.id, {"result": "FOUND", "broker_order_id": resp.get("broker_order_id"), "retried": False, "by": "gateway021", "original_error": resp.get("original_error")})
            state.outcome(s, "RECONCILED", "Reconciled: not retried", f"021's answer was lost ({resp.get('original_error')}); the gateway found the order in 021's order book ({resp.get('broker_order_id')}) and nothing was resent.", "success", proposal_id=ob.proposal_id, broker_order_id=resp.get("broker_order_id"))
    return _record_ack(outbox_id, resp, reconciled=bool(resp.get("reconciled")))


def _resolve_021(outbox_id: str, order: CanonicalOrder) -> dict:
    """The executor lost the gateway's answer: ask the gateway what happened to the tag (it reconciles on its side)."""
    acct = order.account_id
    with session_scope() as s:
        tag = s.get(OutboxEntry, outbox_id).client_order_id
    deadline = time.time() + settings().executor_021_timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            att = broker(acct).attempt(tag, wait_s=10.0)
        except BrokerError as e:
            if e.status == 404:
                return _not_placed(outbox_id, "The 021 gateway never received this order, so nothing reached 021.")
            last_err = e.message
            time.sleep(1.0)
            continue
        except (BrokerTimeout, BrokerUnavailable) as e:
            last_err = str(e)
            time.sleep(1.0)
            continue
        st = att.get("state")
        if st == "SENDING":
            continue
        if st in ("ACCEPTED", "REJECTED") and att.get("broker_order_id"):
            try:
                o = broker(acct).order(att["broker_order_id"])
            except (BrokerTimeout, BrokerUnavailable, BrokerError) as e:
                last_err = str(e)
                time.sleep(1.0)
                continue
            with session_scope() as s:
                ob = s.get(OutboxEntry, outbox_id)
                audit.record(s, "executor", "RECONCILIATION", "outbox", ob.id, {"result": "FOUND", "broker_order_id": o["broker_order_id"], "retried": False, "by": "gateway021"})
                state.outcome(s, "RECONCILED", "Reconciled: not retried", f"The gateway's answer was lost; 021 holds the order ({o['broker_order_id']}, tag {tag}). Nothing was resent.", "success", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"])
            return _record_ack(outbox_id, o, reconciled=True)
        if st == "REJECTED":
            return _refused(outbox_id, BrokerError(422, "REJECTED_BY_021", att.get("error") or "Rejected by 021."))
        if st == "NOT_PLACED":
            return _not_placed(outbox_id, f"021 did not place it ({att.get('error')}).")
        return _needs_review(outbox_id, f"021's outcome for tag {tag} is {st}: {att.get('error')}.", {"candidates": att.get("candidates")})
    return _needs_review(outbox_id, f"The outcome of tag {tag} could not be confirmed with the 021 gateway ({last_err}).")


def _record_ack(outbox_id: str, resp: dict, *, reconciled: bool) -> dict:
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        ob.broker_order_id = resp["broker_order_id"]
        ob.broker_status = resp["status"]
        ob.filled_quantity = resp.get("filled_quantity", 0)
        ob.avg_fill_price_paise = resp.get("avg_fill_price_paise")
        ob.reject_reason = resp.get("reject_reason")
        ob.reconciled = reconciled
        ob.state = "RECONCILED_FOUND" if reconciled else "ACKED"
        if resp["status"] == "REJECTED":
            ob.state = "REJECTED_BY_BROKER"
        audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": ob.state, "broker_order_id": ob.broker_order_id, "status": resp["status"]})
        from countersign.executor.tracker import apply_order_update

        apply_order_update(s, resp)
        return {"ok": resp["status"] != "REJECTED", "outbox_id": ob.id, "broker_order_id": ob.broker_order_id, "state": ob.state, "status": resp["status"]}


def reconcile(outbox_id: str, *, allow_retry: bool, window_s: float | None = None) -> dict:
    window_s = window_s if window_s is not None else settings().reconcile_window_s
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        tag = ob.client_order_id
        acct = s.get(Proposal, ob.proposal_id).account_id
        order = None
        try:
            order = CanonicalOrder.model_validate(_order_from_request(s, ob))
        except Exception:  # noqa: BLE001
            order = None
    if order is not None and order.action != "PLACE":
        # A modification or cancellation has no order of its own: it is confirmed on the target order.
        return _confirm_target(outbox_id, order, sent_ok=False, allow_retry=allow_retry)
    deadline = time.time() + window_s
    found = None
    while time.time() < deadline:
        try:
            rows = broker(acct).orders(status="all", tag=tag)
            if rows:
                found = rows
                break
        except (BrokerTimeout, BrokerUnavailable, BrokerError):
            pass
        time.sleep(0.5)
    if found:
        if len(found) > 1:
            with session_scope() as s:
                state.security_event(s, "critical", "DUPLICATE_AT_BROKER", f"Broker holds {len(found)} orders with tag {tag}.", {"tag": tag}, actor="executor")
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            audit.record(s, "executor", "RECONCILIATION", "outbox", ob.id, {"result": "FOUND", "broker_order_id": found[0]["broker_order_id"], "retried": False})
            state.outcome(s, "RECONCILED", "Reconciled: not retried", f"Order was not retried because the broker already accepted the original order ({found[0]['broker_order_id']}, tag {tag}).", "success", proposal_id=ob.proposal_id, broker_order_id=found[0]["broker_order_id"])
        return _record_ack(outbox_id, found[0], reconciled=True)
    with session_scope() as s:
        ob = s.get(OutboxEntry, outbox_id)
        audit.record(s, "executor", "RECONCILIATION", "outbox", ob.id, {"result": "ABSENT", "attempts": ob.attempts})
        if not allow_retry or ob.attempts >= 2 or order is None:
            ob.state = "FAILED"
            ob.reject_reason = "Broker outcome could not be confirmed; not retried again. Check with the broker before placing a new order."
            _set_step(s, ob.proposal_id, ob.step_no, state="FAILED", reason=ob.reject_reason)
            state.outcome(s, "EXECUTION_BLOCKED", "Order outcome unknown", ob.reject_reason, "danger", proposal_id=ob.proposal_id)
            return {"ok": False, "outbox_id": ob.id, "state": "FAILED"}
        ob.state = "RECONCILED_ABSENT"
        audit.record(s, "executor", "RETRY", "outbox", ob.id, {"reason": "Broker confirmed the order is absent; one safe retry with the same idempotent tag."})
    return _send(outbox_id, order)


def _order_from_request(s, ob: OutboxEntry) -> dict:
    job = s.get(ExecJob, ob.job_id)
    steps = _steps_for(s, job, s.get(Proposal, job.proposal_id))
    for st in steps:
        if st["step_no"] == ob.step_no:
            return st["order"]
    raise KeyError(ob.step_no)


def wait_for_fill(job_id: str, depends_on: int, timeout_s: float = 30.0) -> dict:
    """A dependent step runs once the step it depends on has filled, fully or partly: 021's sandbox fills only part of
    an order and holds the rest, and a step that filled nothing stops the plan. The broker is asked directly every
    2 s, so a missed order update cannot hold the plan back."""
    from countersign.executor import tracker

    deadline = time.time() + timeout_s
    next_ask = time.time() + 1.0
    while time.time() < deadline:
        with session_scope() as s:
            job = s.get(ExecJob, job_id)
            ob = s.execute(select(OutboxEntry).where(OutboxEntry.capability_id == job.capability_id, OutboxEntry.step_no == depends_on)).scalar_one_or_none()
            if ob is None:
                return {"filled": False, "reason": f"Step {depends_on} was never sent."}
            if ob.broker_status == "COMPLETE":
                return {"filled": True}
            if ob.broker_status == "PARTIALLY_FILLED" and (ob.filled_quantity or 0) > 0:
                audit.record(s, "executor", "DEPENDENCY_PARTLY_FILLED", "job", job.id, {"step_no": depends_on, "filled": ob.filled_quantity})
                return {"filled": True, "partial": True}
            if ob.broker_status in ("REJECTED", "CANCELLED") or ob.state in ("REJECTED_BY_BROKER", "FAILED", "NOT_PLACED", "NEEDS_REVIEW"):
                return {"filled": False, "reason": f"Step {depends_on} ended {ob.broker_status or ob.state} (filled {ob.filled_quantity})."}
            if state.kill_switch(s)["engaged"]:
                return {"filled": False, "reason": "Kill switch engaged."}
            tag, acct = ob.client_order_id, s.get(Proposal, ob.proposal_id).account_id
        if time.time() >= next_ask:
            next_ask = time.time() + 2.0
            try:
                for o in broker(acct).orders(status="all", tag=tag):
                    tracker.apply_with_retry(o)
            except Exception:  # noqa: BLE001
                pass  # the tracker's own updates still arrive; the deadline bounds the wait
        time.sleep(0.5)
    return {"filled": False, "reason": f"Step {depends_on} did not fill within {int(timeout_s)}s; dependent steps were not executed."}


def skip_remaining(job_id: str, from_step: int, reason: str) -> None:
    with session_scope() as s:
        job = s.get(ExecJob, job_id)
        for st in s.execute(select(PlanStep).where(PlanStep.plan_id == job.proposal_id, PlanStep.step_no >= from_step)).scalars():
            if st.state == "PENDING":
                st.state = "SKIPPED"
                st.reason = reason
        audit.record(s, "executor", "PLAN_STEPS_SKIPPED", "plan", job.proposal_id, {"from_step": from_step, "reason": reason, "compensating_trades": "none (stop on failure)"})


def finalize(job_id: str) -> None:
    with session_scope() as s:
        job = s.get(ExecJob, job_id)
        if job.status == "RUNNING":
            job.status = "DONE"
            job.finished_at = datetime.now(UTC)
        from countersign.executor.tracker import refresh_proposal_status

        refresh_proposal_status(s, job.proposal_id)
