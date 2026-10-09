"""Applies broker order updates (postbacks + periodic sync) to outbox, plan steps, mandates and proposal state,
and emits human-readable live outcomes. Reports exactly what the broker says: filled, partial, rejected, cancelled."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from countersign.core import audit, events, state
from countersign.core.db import session_scope
from countersign.core.models import BrokerOrderMirror, ExecJob, OrderFill, OutboxEntry, Plan, PlanStep, Proposal, StandingMandate
from countersign.core.money import inr

STEP_FROM_STATUS = {
    "OPEN": "OPEN",
    "TRIGGER_PENDING": "OPEN",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "COMPLETE": "FILLED",
    "REJECTED": "REJECTED",
    "CANCELLED": "CANCELLED",
}


def _ref(o: dict, tag: str | None) -> str:
    """021 has no client order tag: there the id is Countersign's own placement reference, matched by the gateway."""
    return f"Countersign ref {tag}" if o.get("broker") == "021" else f"tag {tag}"


def apply_order_update(s: Session, o: dict) -> None:
    tag = o.get("client_order_id")
    # Lock order for every writer (postback, periodic sync, executor ack): outbox row first, then the mirror row.
    # A consistent order makes concurrent updates of the same order queue instead of deadlocking.
    ob = s.execute(select(OutboxEntry).where(OutboxEntry.client_order_id == tag).with_for_update()).scalar_one_or_none() if tag else None
    mirror = s.get(BrokerOrderMirror, o["broker_order_id"])
    prev_status = mirror.status if mirror else None
    prev_filled = mirror.filled_quantity if mirror else 0
    if mirror is None:
        mirror = BrokerOrderMirror(broker_order_id=o["broker_order_id"], account_id=o.get("account_id") or "UNKNOWN", instrument_id=o["instrument_id"], side=o["side"], quantity=o["quantity"], status=o["status"])
        s.add(mirror)
    mirror.client_order_id = tag
    mirror.filled_quantity = o["filled_quantity"]
    mirror.avg_fill_price_paise = o.get("avg_fill_price_paise")
    mirror.status = o["status"]
    mirror.reject_reason = o.get("reject_reason")
    if o["filled_quantity"] > prev_filled:
        s.merge(OrderFill(trade_id=f"T{o['broker_order_id']}-{o['filled_quantity']}", broker_order_id=o["broker_order_id"], quantity=o["filled_quantity"] - prev_filled, price_paise=o.get("avg_fill_price_paise") or 0, filled_at=datetime.now(UTC)))
    if ob is None:
        return
    if ob.state in ("NOT_PLACED", "NEEDS_REVIEW", "UNKNOWN") and not ob.broker_order_id:
        # The broker now shows an order for a placement that was reported as not placed or unresolved (021 accepted
        # it late, or the gateway resolved an ambiguity). Report it plainly; nothing was resent automatically.
        was = ob.state
        ob.state = "RECONCILED_FOUND"
        ob.reconciled = True
        audit.record(s, "executor", "RECONCILIATION", "outbox", ob.id, {"result": "LATE_FOUND", "was": was, "broker_order_id": o["broker_order_id"]})
        state.outcome(s, "RECONCILED", "Order found after all", f"The broker now shows order {o['broker_order_id']} ({_ref(o, tag)}), which had been reported {was.replace('_', ' ').lower()}. It was not resent automatically; check that you did not place it again.", "warning", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"])
    ob.broker_order_id = ob.broker_order_id or o["broker_order_id"]
    ob.broker_status = o["status"]
    ob.filled_quantity = o["filled_quantity"]
    ob.avg_fill_price_paise = o.get("avg_fill_price_paise")
    if o.get("reject_reason"):
        ob.reject_reason = o["reject_reason"]
    step = s.get(PlanStep, (ob.proposal_id, ob.step_no))
    if step:
        step.state = STEP_FROM_STATUS.get(o["status"], step.state)
        step.broker_order_id = o["broker_order_id"]
        step.client_order_id = tag
        step.filled_quantity = o["filled_quantity"]
        step.avg_fill_price_paise = o.get("avg_fill_price_paise")
        step.reason = o.get("reject_reason")
    if prev_status == o["status"] and prev_filled == o["filled_quantity"]:
        return
    p = s.get(Proposal, ob.proposal_id)
    sym = o.get("symbol", "")
    acct = p.account_id if p else (o.get("account_id") or "*")
    st = o["status"]
    if st == "OPEN" and prev_status is None:
        state.outcome(s, "ORDER_ACCEPTED", f"Order accepted: {o['side']} {o['quantity']} {sym}", f"Broker order {o['broker_order_id']} ({_ref(o, tag)}) is open.", "info", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"], account_id=acct)
    elif st == "PARTIALLY_FILLED" or (st == "CANCELLED" and 0 < o["filled_quantity"] < o["quantity"]):
        if st == "PARTIALLY_FILLED":
            state.outcome(s, "ORDER_PARTIALLY_FILLED", f"Partially filled: {o['filled_quantity']}/{o['quantity']} {sym}", f"Average {inr(o.get('avg_fill_price_paise'))}. {o.get('reject_reason') or ''}".strip(), "warning", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"], account_id=acct)
            audit.record(s, "broker", "PARTIAL_FILL", "order", o["broker_order_id"], {"filled": o["filled_quantity"], "quantity": o["quantity"]})
        else:
            state.outcome(s, "ORDER_CANCELLED", f"Remainder cancelled: {o['filled_quantity']}/{o['quantity']} {sym} filled", o.get("reject_reason") or "Remaining quantity cancelled.", "warning", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"], account_id=acct)
            audit.record(s, "broker", "ORDER_CANCELLED", "order", o["broker_order_id"], {"filled": o["filled_quantity"]})
    elif st == "COMPLETE" and prev_status == "CANCELLED":
        # Seen on the 021 sandbox: an order confirmed Cancelled later shows Executed. Reported as the broker states it.
        state.outcome(s, "ORDER_FILLED", f"Filled after its cancellation: {o['side']} {o['quantity']} {sym}", f"The broker had confirmed order {o['broker_order_id']} as cancelled and now reports it executed at an average of {inr(o.get('avg_fill_price_paise'))}. Check your position; nothing was resent.", "warning", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"], account_id=acct)
        audit.record(s, "broker", "FILL_AFTER_CANCEL", "order", o["broker_order_id"], {"filled": o["filled_quantity"], "avg_price_paise": o.get("avg_fill_price_paise")})
    elif st == "COMPLETE":
        state.outcome(s, "ORDER_FILLED", f"Filled: {o['side']} {o['quantity']} {sym}", f"Average {inr(o.get('avg_fill_price_paise'))} (order {o['broker_order_id']}).", "success", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"], account_id=acct)
        audit.record(s, "broker", "FILL", "order", o["broker_order_id"], {"filled": o["filled_quantity"], "avg_price_paise": o.get("avg_fill_price_paise")})
    elif st == "REJECTED":
        state.outcome(s, "ORDER_REJECTED", f"Rejected: {o['side']} {o['quantity']} {sym}", o.get("reject_reason") or "Rejected by broker.", "danger", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"], account_id=acct)
        audit.record(s, "broker", "REJECTION", "order", o["broker_order_id"], {"reason": o.get("reject_reason")})
    elif st == "CANCELLED":
        state.outcome(s, "ORDER_CANCELLED", f"Cancelled: {o['side']} {o['quantity']} {sym}", f"Order {o['broker_order_id']} cancelled.", "info", proposal_id=ob.proposal_id, broker_order_id=o["broker_order_id"], account_id=acct)
    refresh_proposal_status(s, ob.proposal_id)


def refresh_proposal_status(s: Session, proposal_id: str) -> None:
    p = s.get(Proposal, proposal_id)
    if p is None:
        return
    job = s.execute(select(ExecJob).where(ExecJob.proposal_id == p.id).order_by(ExecJob.created_at.desc()).limit(1)).scalar_one_or_none()
    if job is None:
        return
    if p.kind == "MANDATE":
        m = s.get(StandingMandate, job.mandate_id) if job.mandate_id else None
        ob = s.execute(select(OutboxEntry).where(OutboxEntry.job_id == job.id)).scalars().first()
        if m and ob and m.state in ("EXECUTING", "FIRED"):
            if ob.broker_status in ("OPEN", "PARTIALLY_FILLED", "COMPLETE"):
                if m.state != "FIRED":
                    m.state = "FIRED"
                    m.fired_at = datetime.now(UTC)
                    audit.record(s, "executor", "MANDATE_FIRED", "mandate", m.id, {"broker_order_id": ob.broker_order_id, "trigger_event_id": m.trigger_event_id})
            if ob.broker_status == "COMPLETE":
                m.state = "COMPLETED"
            if ob.broker_status == "REJECTED" or ob.state in ("REJECTED_BY_BROKER", "FAILED", "NOT_PLACED", "NEEDS_REVIEW"):
                m.state = "NEEDS_REVIEW" if ob.state == "NEEDS_REVIEW" else "FAILED"
                m.reason = ob.reject_reason
            events.emit_mandate(m)
        return
    outs = s.execute(select(OutboxEntry).where(OutboxEntry.job_id == job.id)).scalars().all()
    if p.kind == "ORDER":
        if not outs:
            return
        ob = outs[0]
        new = p.status
        if ob.broker_status == "COMPLETE":
            new = "COMPLETED"
        elif ob.broker_status == "REJECTED" or ob.state in ("REJECTED_BY_BROKER", "FAILED", "NOT_PLACED", "NEEDS_REVIEW"):
            new = "FAILED"
        elif ob.broker_status == "CANCELLED":
            new = "PARTIALLY_COMPLETED" if ob.filled_quantity > 0 else ("COMPLETED" if s.get(Proposal, p.id).canonical.get("action") == "CANCEL" else "FAILED")
        elif ob.broker_status in ("OPEN", "PARTIALLY_FILLED", "TRIGGER_PENDING"):
            new = "EXECUTING"
        if p.canonical.get("action") in ("CANCEL", "MODIFY") and ob.state in ("ACKED", "RECONCILED_FOUND"):
            new = "COMPLETED"
        if new != p.status:
            p.status = new
            events.emit_proposal(p)
        return
    plan = s.get(Plan, p.id)
    steps = s.execute(select(PlanStep).where(PlanStep.plan_id == p.id)).scalars().all()
    states = [st.state for st in steps]
    if any(x in ("PENDING", "SUBMITTED", "OPEN", "PARTIALLY_FILLED") for x in states) and job.status == "RUNNING":
        return
    if any(x in ("PENDING", "SUBMITTED", "OPEN") for x in states) and job.status != "DONE":
        return
    filled_like = [x for x in states if x == "FILLED" or (x == "CANCELLED" and any(st.filled_quantity for st in steps if st.state == x))]
    if all(x == "FILLED" or (x == "CANCELLED" and p.canonical["steps"][i]["order"]["action"] == "CANCEL") for i, x in enumerate(states)):
        new_plan, new_p = "COMPLETED", "COMPLETED"
    elif any(x in ("FILLED", "PARTIALLY_FILLED", "OPEN") for x in states) or filled_like:
        new_plan, new_p = ("PARTIALLY_COMPLETED", "PARTIALLY_COMPLETED")
        if any(x in ("REJECTED", "FAILED", "NOT_PLACED", "NEEDS_REVIEW") for x in states):
            new_plan = "PARTIALLY_FAILED"
    else:
        new_plan, new_p = "PARTIALLY_FAILED", "FAILED"
    if plan and plan.state != new_plan and job.status in ("DONE", "REJECTED", "HALTED"):
        plan.state = new_plan
        plan.completed_at = datetime.now(UTC)
        p.status = new_p
        audit.record(s, "executor", "PLAN_STATE", "plan", p.id, {"to": new_plan, "steps": states})
        kind = {"COMPLETED": "PLAN_COMPLETED", "PARTIALLY_COMPLETED": "PLAN_PARTIALLY_COMPLETED"}.get(new_plan, "PLAN_FAILED")
        detail = ", ".join(f"step {st.step_no}: {st.state.lower().replace('_', ' ')}" + (f" ({st.filled_quantity} filled)" if st.filled_quantity else "") for st in steps)
        state.outcome(s, kind, f"Plan {new_plan.lower().replace('_', ' ')}", detail + ". No compensating trades were made.", "success" if new_plan == "COMPLETED" else "warning", proposal_id=p.id, account_id=p.account_id)
        events.emit_proposal(p)


def apply_with_retry(o: dict, attempts: int = 3) -> None:
    """Applies one broker update in its own transaction; retries transient lock conflicts (deadlock, duplicate
    mirror insert) because the update is idempotent: it only moves the mirror towards what the broker reports."""
    for i in range(attempts):
        try:
            with session_scope() as s:
                apply_order_update(s, o)
            return
        except (OperationalError, IntegrityError) as e:
            if i == attempts - 1:
                print("tracker apply error:", type(e.orig).__name__ if getattr(e, "orig", None) else e)
                return
            time.sleep(0.05 * (i + 1))


def listen_forever() -> None:
    while True:
        try:
            ps = events.client().pubsub(ignore_subscribe_messages=True)
            ps.subscribe(events.CH_BROKER_UPDATES)
            for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    apply_with_retry(json.loads(msg["data"])["order"])
                except Exception as e:  # noqa: BLE001
                    print("tracker apply error:", e)
        except Exception:
            time.sleep(1.0)


def sync_forever(broker) -> None:
    """Periodic reconciliation of non-terminal orders in case a postback was missed."""
    while True:
        time.sleep(3.0)
        try:
            with session_scope() as s:
                pairs = s.execute(select(OutboxEntry.client_order_id, Proposal.account_id).join(Proposal, Proposal.id == OutboxEntry.proposal_id).where(OutboxEntry.broker_status.in_(["OPEN", "PARTIALLY_FILLED", "TRIGGER_PENDING"]))).all()
            for tag, acct in pairs:
                rows = broker(acct).orders(status="all", tag=tag)
                for o in rows:
                    apply_with_retry(o)
        except Exception:  # noqa: BLE001
            pass


def start(broker) -> None:
    threading.Thread(target=listen_forever, daemon=True, name="tracker-listen").start()
    threading.Thread(target=sync_forever, args=(broker,), daemon=True, name="tracker-sync").start()
