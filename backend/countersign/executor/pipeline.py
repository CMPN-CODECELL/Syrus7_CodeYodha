"""Executor pipeline. Runs ONLY in the executor process, which alone holds the broker write key and never imports
any LLM code. Every check is repeated here from first principles; nothing upstream is trusted."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime

from sqlalchemy import select

from countersign.core import audit, capabilities, events, market, state
from countersign.core.approval_crypto import CountersignError, payload_hash, verify_server_attestation, verify_webauthn
from countersign.core.broker_client import BrokerError, BrokerTimeout, BrokerUnavailable, OrderExecutionProvider
from countersign.core.canonical import canonical_hash
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import (
    Account,
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
            if owner is None or owner.user_id != a.user_id or p.user_id != a.user_id:
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
        if ob is not None and ob.state in ("ACKED", "RECONCILED_FOUND", "REJECTED_BY_BROKER", "FAILED"):
            return {"ok": ob.state in ("ACKED", "RECONCILED_FOUND"), "outbox_id": ob.id, "broker_order_id": ob.broker_order_id, "state": ob.state}
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
    if prior_state in ("SENDING", "UNKNOWN", "RECONCILED_ABSENT"):
        return reconcile(outbox_id, allow_retry=True)
    return _send(outbox_id, order)


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
    try:
        if order.action == "PLACE":
            body.pop("action", None)
            body.pop("target_broker_order_id", None)
            resp = broker(acct).place(body)
        elif order.action == "MODIFY":
            resp = broker(acct).modify(order.target_broker_order_id, {"limit_price_paise": order.limit_price_paise})
        else:
            resp = broker(acct).cancel(order.target_broker_order_id)
    except BrokerTimeout:
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            ob.state = "UNKNOWN"
            audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "TIMEOUT", "note": "Outcome unknown; reconciling against the broker order book before any retry."})
            state.outcome(s, "RECONCILED", "Broker response lost", f"Order {ob.client_order_id}: no response from the broker. Reconciling against the order book instead of resending.", "warning", proposal_id=ob.proposal_id)
        return reconcile(outbox_id, allow_retry=True)
    except (BrokerUnavailable, BrokerError) as e:
        not_accepted = isinstance(e, BrokerUnavailable) or (isinstance(e, BrokerError) and e.code == "NOT_ACCEPTED")
        with session_scope() as s:
            ob = s.get(OutboxEntry, outbox_id)
            if not_accepted:
                ob.state = "UNKNOWN"
                audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "NOT_ACCEPTED", "detail": str(e)})
            else:
                ob.state = "REJECTED_BY_BROKER"
                ob.reject_reason = e.message
                audit.record(s, "executor", "BROKER_RESPONSE", "outbox", ob.id, {"result": "ERROR", "code": e.code, "message": e.message})
                _set_step(s, ob.proposal_id, ob.step_no, state="REJECTED", reason=e.message)
                state.outcome(s, "ORDER_REJECTED", "Broker refused the order", e.message, "danger", proposal_id=ob.proposal_id)
        if not_accepted:
            return reconcile(outbox_id, allow_retry=True, window_s=1.5)
        return {"ok": False, "outbox_id": outbox_id, "state": "REJECTED_BY_BROKER"}
    return _record_ack(outbox_id, resp, reconciled=False)


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


def wait_for_fill(job_id: str, depends_on: int, timeout_s: float = 20.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with session_scope() as s:
            job = s.get(ExecJob, job_id)
            ob = s.execute(select(OutboxEntry).where(OutboxEntry.capability_id == job.capability_id, OutboxEntry.step_no == depends_on)).scalar_one_or_none()
            if ob is None:
                return {"filled": False, "reason": f"Step {depends_on} was never sent."}
            if ob.broker_status == "COMPLETE":
                return {"filled": True}
            if ob.broker_status in ("REJECTED", "CANCELLED") or ob.state in ("REJECTED_BY_BROKER", "FAILED"):
                return {"filled": False, "reason": f"Step {depends_on} ended {ob.broker_status or ob.state} (filled {ob.filled_quantity})."}
            if state.kill_switch(s)["engaged"]:
                return {"filled": False, "reason": "Kill switch engaged."}
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
