"""Proposal service: the only path from an intent (AI, parser, or attacker) to something a trader can approve.

intent (untrusted) -> builder (provenance, misread guard) -> canonical object -> deterministic verifier
-> Proposal row (BLOCKED or AWAITING_APPROVAL). Nothing here can execute anything."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from countersign.core import audit, events, state
from countersign.core.canonical import canonical_hash, short
from countersign.core.models import Approval, ExecJob, OutboxEntry, Plan, PlanStep, Proposal, StandingMandate
from countersign.core.parser import Facts, extract_facts
from countersign.core.schemas import CanonicalMandate, CanonicalOrder, CanonicalPlan, MandateIntent, OrderIntent, PlanIntent, iso
from countersign.core.verifier import (
    AccountState,
    build_mandate,
    build_order,
    build_plan,
    describe_predicate,
    mandate_summary,
    summarize,
    verify_order,
    verify_plan,
)


@dataclass
class Outcome:
    proposal: Proposal | None = None
    clarify: tuple[str, list[str]] | None = None


def _step_view(step_no: int, order: CanonicalOrder, fields, preview, issues, depends_on=None) -> dict:
    return {
        "step_no": step_no,
        "canonical": order.model_dump(mode="json"),
        "summary": summarize(order),
        "fields": [f.model_dump(mode="json") for f in fields],
        "preview": preview.model_dump(mode="json") if preview else None,
        "issues": [i.model_dump(mode="json") for i in issues],
        "depends_on": depends_on,
    }


def _persist(
    s: Session,
    *,
    kind: str,
    canonical: dict,
    summary: str,
    steps_view: list[dict],
    issues: list[dict],
    disagreements: list[dict],
    user_id: str,
    account_id: str,
    engine: str,
    user_text: str,
    conversation_id: str | None,
    origin: str,
) -> Proposal:
    blocked = any(i["severity"] == "BLOCK" for i in issues) or any(
        i["severity"] == "BLOCK" for sv in steps_view for i in sv["issues"]
    )
    p = Proposal(
        kind=kind,
        status="BLOCKED" if blocked else "AWAITING_APPROVAL",
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
        canonical=canonical,
        canonical_hash=canonical_hash(canonical),
        summary=summary,
        steps_view=steps_view,
        issues=issues,
        interpretation={"user_text": user_text, "engine": engine, "disagreements": disagreements},
        engine=engine,
        source_text=user_text,
        origin=origin,
    )
    s.add(p)
    s.flush()
    actor = "ai" if engine not in ("deterministic",) else "parser"
    audit.record(s, actor, "TYPED_INTENT", "proposal", p.id, {"engine": engine, "user_text": user_text[:500]})
    audit.record(s, "verifier", "VERIFICATION_RESULT", "proposal", p.id, {"status": p.status, "issues": [i.get("code") for i in issues] + [i["code"] for sv in steps_view for i in sv["issues"]]})
    audit.record(s, "verifier", "CANONICAL_CREATED", "proposal", p.id, {"kind": kind, "canonical_hash": p.canonical_hash, "summary": summary})
    events.emit_proposal(p)
    return p


def create_order(
    s: Session,
    intent: OrderIntent,
    *,
    user_text: str,
    account: AccountState,
    user_id: str,
    engine: str,
    conversation_id: str | None = None,
    origin: str = "chat",
    facts: Facts | None = None,
) -> Outcome:
    facts = facts or extract_facts(user_text)
    br = build_order(s, intent, facts, account, engine=engine)
    if br.clarify and br.canonical is None:
        return Outcome(clarify=br.clarify)
    issues = list(br.issues)
    v_issues, preview = verify_order(s, br.canonical, account)
    issues += v_issues
    sv = _step_view(1, br.canonical, br.fields, preview, issues)
    p = _persist(
        s,
        kind="ORDER",
        canonical=br.canonical.model_dump(mode="json"),
        summary=summarize(br.canonical),
        steps_view=[sv],
        issues=[],
        disagreements=[d.model_dump() for d in br.disagreements],
        user_id=user_id,
        account_id=account.account_id,
        engine=engine,
        user_text=user_text,
        conversation_id=conversation_id,
        origin=origin,
    )
    return Outcome(proposal=p)


def create_plan(
    s: Session,
    plan_intent: PlanIntent,
    *,
    user_text: str,
    account: AccountState,
    user_id: str,
    engine: str,
    conversation_id: str | None = None,
    origin: str = "chat",
) -> Outcome:
    import dataclasses
    import re

    from countersign.core.parser import ACTION_RE

    full_facts = extract_facts(user_text)
    clauses = [c for c in re.split(r"\s*(?:,\s*)?\b(?:and then|then|and)\b\s*", user_text, flags=re.I) if ACTION_RE.search(c)]
    per_step = [extract_facts(c) for c in clauses] if len(clauses) == len(plan_intent.steps) else None
    relaxed = dataclasses.replace(full_facts, quantity=None, price=None, lots=None, trigger=None)
    orders: list[CanonicalOrder] = []
    builds = []
    depends: list[int | None] = []
    last_cancel = None
    for idx, step in enumerate(plan_intent.steps):
        replace_source = None
        if step.target_order_ref and step.target_order_ref.startswith("REPLACE:") and last_cancel is not None:
            replace_source = {
                "instrument_id": last_cancel.instrument.instrument_id,
                "side": last_cancel.side.value,
                "quantity": last_cancel.quantity,
            }
            step = step.model_copy(update={"target_order_ref": None})
        part_facts = per_step[idx] if per_step else relaxed
        if per_step and not part_facts.symbols and replace_source is None:
            part_facts = dataclasses.replace(part_facts, symbols=full_facts.symbols)
        br = build_order(s, step, part_facts, account, engine=engine, replace_source=replace_source)
        if br.canonical is None:
            q = br.clarify[0] if br.clarify else "I could not build this step."
            return Outcome(clarify=(f"Step {idx + 1}: {q}", br.clarify[1] if br.clarify else []))
        if br.canonical.action == "CANCEL":
            last_cancel = br.canonical
        orders.append(br.canonical)
        builds.append(br)
        prev_sell = idx > 0 and orders[idx - 1].side.value == "SELL" and orders[idx - 1].action == "PLACE"
        depends.append(idx if (plan_intent.sequential and idx > 0 and (prev_sell or orders[idx - 1].action == "CANCEL")) else None)
    plan = build_plan(orders, account.account_id, depends)
    per_issues, previews, plan_issues = verify_plan(s, plan, account)
    steps_view = []
    for i, st in enumerate(plan.steps):
        steps_view.append(_step_view(st.step_no, st.order, builds[i].fields, previews[i], builds[i].issues + per_issues[i], st.depends_on))
    disagreements = [d.model_dump() for b in builds for d in b.disagreements]
    summary = " ; then ".join(f"({st.step_no}) {summarize(st.order)}" for st in plan.steps)
    p = _persist(
        s,
        kind="PLAN",
        canonical=plan.model_dump(mode="json"),
        summary=f"Plan of {len(plan.steps)} steps: {summary}",
        steps_view=steps_view,
        issues=[i.model_dump(mode="json") for i in plan_issues],
        disagreements=disagreements,
        user_id=user_id,
        account_id=account.account_id,
        engine=engine,
        user_text=user_text,
        conversation_id=conversation_id,
        origin=origin,
    )
    s.add(Plan(id=p.id, state="BLOCKED" if p.status == "BLOCKED" else "AWAITING_APPROVAL", plan_hash=p.canonical_hash))
    for st in plan.steps:
        s.add(PlanStep(plan_id=p.id, step_no=st.step_no, canonical=st.order.model_dump(mode="json"), step_hash=canonical_hash(st.order), depends_on=st.depends_on, state="PENDING"))
    audit.record(s, "verifier", "PLAN_STATE", "plan", p.id, {"to": "AWAITING_APPROVAL" if p.status != "BLOCKED" else "BLOCKED", "plan_hash": p.canonical_hash})
    return Outcome(proposal=p)


def create_mandate(
    s: Session,
    mi: MandateIntent,
    *,
    user_text: str,
    account: AccountState,
    user_id: str,
    engine: str,
    conversation_id: str | None = None,
    origin: str = "chat",
) -> Outcome:
    facts = extract_facts(user_text)
    mandate, br, m_issues = build_mandate(s, mi, facts, account, engine=engine)
    if mandate is None:
        return Outcome(clarify=br.clarify or ("I could not build that standing instruction.", []))
    steps_view = []
    if mandate.template is not None:
        t_issues, preview = verify_order(s, mandate.template, account, skip_session=True)
        steps_view.append(_step_view(1, mandate.template, br.fields, preview, list(br.issues) + t_issues))
    p = _persist(
        s,
        kind="MANDATE",
        canonical=mandate.model_dump(mode="json"),
        summary=mandate_summary(mandate),
        steps_view=steps_view,
        issues=[i.model_dump(mode="json") for i in m_issues],
        disagreements=[d.model_dump() for d in br.disagreements],
        user_id=user_id,
        account_id=account.account_id,
        engine=engine,
        user_text=user_text,
        conversation_id=conversation_id,
        origin=origin,
    )
    m = StandingMandate(
        proposal_id=p.id,
        user_id=user_id,
        account_id=account.account_id,
        instrument_id=mandate.instrument.instrument_id,
        kind=mandate.kind,
        predicate=mandate.predicate.model_dump(),
        template=mandate.template.model_dump(mode="json") if mandate.template else None,
        max_quantity=mandate.max_quantity,
        price_limit_paise=mandate.price_limit_paise,
        mandate_hash=p.canonical_hash,
        one_shot=True,
        state="PENDING_APPROVAL",
        expires_at=datetime.fromisoformat(mandate.expires_at.replace("Z", "+00:00")),
    )
    s.add(m)
    s.flush()
    audit.record(s, "verifier", "MANDATE_CREATED", "mandate", m.id, {"proposal_id": p.id, "summary": p.summary})
    return Outcome(proposal=p)


# ------------------------------------------------------------------ views


def mandate_view(m: StandingMandate, canonical: dict) -> dict:
    cm = CanonicalMandate.model_validate(canonical)
    return {
        "mandate_id": m.id,
        "proposal_id": m.proposal_id,
        "kind": m.kind,
        "instrument": cm.instrument.model_dump(mode="json"),
        "condition_text": describe_predicate(cm),
        "predicate": m.predicate,
        "template": m.template,
        "max_quantity": m.max_quantity,
        "price_limit_paise": m.price_limit_paise,
        "one_shot": m.one_shot,
        "expires_at": iso(m.expires_at),
        "state": m.state,
        "created_at": iso(m.created_at),
        "activated_at": iso(m.activated_at),
        "triggered_at": iso(m.triggered_at),
        "fired_at": iso(m.fired_at),
        "trigger_event_id": m.trigger_event_id,
        "last_evaluated_at": iso(m.last_evaluated_at),
        "reason": m.reason,
    }


def approval_view(a: Approval | None) -> dict | None:
    if a is None:
        return None
    label = None
    if a.method == "webauthn" and a.credential_id:
        from countersign.core.models import DeviceCredential
        from countersign.core.db import session_scope

        with session_scope() as s2:
            dc = s2.execute(select(DeviceCredential).where(DeviceCredential.credential_id == a.credential_id)).scalar_one_or_none()
            label = dc.label if dc else None
    return {
        "approval_id": a.id,
        "status": a.status,
        "method": a.method,
        "assurance": "device_signed" if a.method == "webauthn" else "server_attested",
        "issued_at": iso(a.issued_at),
        "expires_at": iso(a.expires_at),
        "verified_at": iso(a.verified_at),
        "credential_label": label,
        "payload_hash": a.payload_hash,
        "failure_reason": a.failure_reason,
    }


def view(s: Session, p: Proposal) -> dict:
    approval = s.execute(select(Approval).where(Approval.proposal_id == p.id).order_by(Approval.issued_at.desc()).limit(1)).scalar_one_or_none()
    job = s.execute(select(ExecJob).where(ExecJob.proposal_id == p.id).order_by(ExecJob.created_at.desc()).limit(1)).scalar_one_or_none()
    execution = None
    if job:
        outbox = s.execute(select(OutboxEntry).where(OutboxEntry.job_id == job.id).order_by(OutboxEntry.step_no)).scalars().all()
        execution = {
            "job_id": job.id,
            "status": job.status,
            "reason": job.reason,
            "reason_code": job.reason_code,
            "outbox": [
                {"client_order_id": o.client_order_id, "step_no": o.step_no, "state": o.state, "broker_order_id": o.broker_order_id, "attempts": o.attempts, "reconciled": o.reconciled}
                for o in outbox
            ],
        }
    steps = [dict(sv) for sv in (p.steps_view or [])]
    if p.kind == "PLAN":
        rows = {r.step_no: r for r in s.execute(select(PlanStep).where(PlanStep.plan_id == p.id)).scalars()}
        for sv in steps:
            r = rows.get(sv["step_no"])
            if r:
                sv.update(state=r.state, broker_order_id=r.broker_order_id, client_order_id=r.client_order_id, filled_quantity=r.filled_quantity, avg_fill_price_paise=r.avg_fill_price_paise, reason=r.reason)
    elif p.kind == "ORDER" and job:
        ob = s.execute(select(OutboxEntry).where(OutboxEntry.job_id == job.id)).scalars().first()
        if ob and steps:
            st = {"ACKED": "OPEN", "RECONCILED_FOUND": "OPEN", "REJECTED_BY_BROKER": "REJECTED"}.get(ob.state, "SUBMITTED")
            bs = ob.broker_status
            if bs == "COMPLETE":
                st = "FILLED"
            elif bs in ("PARTIALLY_FILLED",):
                st = "PARTIALLY_FILLED"
            elif bs in ("REJECTED", "CANCELLED"):
                st = bs
            steps[0].update(state=st, broker_order_id=ob.broker_order_id, client_order_id=ob.client_order_id, filled_quantity=ob.filled_quantity, avg_fill_price_paise=ob.avg_fill_price_paise, reason=ob.reject_reason)
    mandate = None
    if p.kind == "MANDATE":
        m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == p.id)).scalar_one_or_none()
        if m:
            mandate = mandate_view(m, p.canonical)
    all_issues = list(p.issues or []) + [i for sv in steps for i in sv["issues"]]
    ks = state.kill_switch(s)
    can = p.status == "AWAITING_APPROVAL" and not any(i["severity"] == "BLOCK" for i in all_issues) and not ks["engaged"]
    return {
        "id": p.id,
        "kind": p.kind,
        "status": p.status,
        "summary": p.summary,
        "canonical_hash": p.canonical_hash,
        "short_hash": short(p.canonical_hash),
        "steps": steps,
        "mandate": mandate,
        "failure_policy": "STOP_ON_FAILURE" if p.kind == "PLAN" else None,
        "issues": p.issues or [],
        "can_approve": can,
        "interpretation": p.interpretation,
        "approval": approval_view(approval),
        "execution": execution,
        "created_at": iso(p.created_at),
        "updated_at": iso(p.updated_at),
    }
