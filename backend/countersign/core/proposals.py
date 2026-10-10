"""Proposal service: the only path from an intent (AI, parser, or attacker) to something a trader can approve.

intent (untrusted) -> builder (provenance, misread guard) -> canonical object -> deterministic verifier
-> Proposal row (BLOCKED or AWAITING_APPROVAL). Nothing here can execute anything."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from decimal import Decimal

from countersign.core import audit, events, state
from countersign.core.canonical import canonical_hash, short
from countersign.core.models import Approval, ExecJob, OutboxEntry, Plan, PlanStep, Proposal, StandingMandate
from countersign.core.parser import Facts, detect_cancel_all, detect_exit_losers, detect_rebalance, extract_facts
from countersign.core.schemas import CanonicalMandate, CanonicalOrder, CanonicalPlan, MandateIntent, OrderIntent, PlanIntent, iso
from countersign.core.verifier import (
    MAX_GENERATED_PLAN_STEPS,
    AccountState,
    PlanDraft,
    build_cancel_all_plan,
    build_exit_plan,
    build_mandate,
    build_order,
    build_plan,
    build_rebalance_plan,
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
    notice: tuple[str, str] | None = None  # (title, message): nothing to draft, and exactly why


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
    p = _persist_plan(
        s, plan, steps_view, plan_issues, f"Plan of {len(plan.steps)} steps: {summary}", disagreements,
        user_id=user_id, account=account, engine=engine, user_text=user_text, conversation_id=conversation_id, origin=origin,
    )
    return Outcome(proposal=p)


def _persist_plan(
    s: Session,
    plan: CanonicalPlan,
    steps_view: list[dict],
    plan_issues: list,
    summary: str,
    disagreements: list[dict],
    *,
    user_id: str,
    account: AccountState,
    engine: str,
    user_text: str,
    conversation_id: str | None,
    origin: str,
) -> Proposal:
    p = _persist(
        s,
        kind="PLAN",
        canonical=plan.model_dump(mode="json"),
        summary=summary,
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
    return p


def _generated_plan(
    s: Session,
    draft: PlanDraft,
    *,
    user_text: str,
    account: AccountState,
    user_id: str,
    engine: str,
    conversation_id: str | None,
    origin: str,
    kind: str,
) -> Outcome:
    """Persist a verifier-computed plan: independent steps (no dependencies), one signature for all of them."""
    if draft.message is not None:
        audit.record(s, "verifier", "NO_PLAN_NEEDED", "account", account.account_id, {"kind": kind, "reason": draft.message[:300]})
        return Outcome(notice=(draft.title or "Nothing to do", draft.message))
    plan = build_plan(draft.orders, account.account_id, [None] * len(draft.orders))
    per_issues, previews, plan_issues = verify_plan(s, plan, account, max_steps=MAX_GENERATED_PLAN_STEPS, independent=True)
    plan_blocks = [i for i in plan_issues if i.severity == "BLOCK"]
    plan_issues = list(draft.notes) + [i for i in plan_issues if i.code != "PLAN_USES_PROCEEDS"]
    steps_view = []
    for i, st in enumerate(plan.steps):
        step_issues = draft.issues[i] + per_issues[i]
        if i == 0:
            # The plan card lists issues per step, so the method, the resulting weights and any plan-wide block
            # (for example too many steps) travel with step 1.
            step_issues = list(draft.notes) + plan_blocks + step_issues
        view = _step_view(st.step_no, st.order, draft.fields[i], previews[i], step_issues, None)
        if draft.summaries:
            view["summary"] = draft.summaries[i]
        steps_view.append(view)
    summary = " ; ".join(f"({st.step_no}) {sv['summary']}" for st, sv in zip(plan.steps, steps_view, strict=True))
    p = _persist_plan(
        s, plan, steps_view, plan_issues, f"{draft.title}: {summary}", [],
        user_id=user_id, account=account, engine=engine, user_text=user_text, conversation_id=conversation_id, origin=origin,
    )
    return Outcome(proposal=p)


def create_exit_plan(
    s: Session,
    *,
    user_text: str,
    account: AccountState,
    user_id: str,
    engine: str,
    conversation_id: str | None = None,
    origin: str = "chat",
) -> Outcome:
    """'Exit all my losing intraday positions'. The scope comes from the trader's words, never from the AI."""
    req = detect_exit_losers(user_text)
    if req is None:
        audit.record(s, "verifier", "ACTION_NOT_REQUESTED", "account", account.account_id, {"kind": "EXIT_LOSERS", "user_text": user_text[:300]})
        return Outcome(notice=("Nothing drafted", "Your message does not ask to exit losing positions, so no exit plan was drafted."))
    draft = build_exit_plan(s, account, extract_facts(user_text), intraday_only=req["intraday_only"])
    return _generated_plan(s, draft, user_text=user_text, account=account, user_id=user_id, engine=engine, conversation_id=conversation_id, origin=origin, kind="EXIT_LOSERS")


def create_cancel_all_plan(
    s: Session,
    *,
    user_text: str,
    account: AccountState,
    user_id: str,
    engine: str,
    conversation_id: str | None = None,
    origin: str = "chat",
) -> Outcome:
    """'Cancel all my open orders' (or 'cancel all my RELIANCE orders'). The scope comes from the trader's words."""
    req = detect_cancel_all(user_text)
    if req is None:
        audit.record(s, "verifier", "ACTION_NOT_REQUESTED", "account", account.account_id, {"kind": "CANCEL_ALL", "user_text": user_text[:300]})
        return Outcome(notice=("Nothing drafted", "Your message does not ask to cancel all your open orders, so no cancel plan was drafted."))
    draft = build_cancel_all_plan(s, account, symbol=req["symbol"])
    return _generated_plan(s, draft, user_text=user_text, account=account, user_id=user_id, engine=engine, conversation_id=conversation_id, origin=origin, kind="CANCEL_ALL")


def create_rebalance_plan(
    s: Session,
    *,
    user_text: str,
    account: AccountState,
    user_id: str,
    engine: str,
    max_pct: Decimal | None = None,
    conversation_id: str | None = None,
    origin: str = "chat",
) -> Outcome:
    """'Rebalance so no single stock is more than 20% of my portfolio'. The cap must be the one the trader stated."""
    req = detect_rebalance(user_text)
    if req is None:
        audit.record(s, "verifier", "ACTION_NOT_REQUESTED", "account", account.account_id, {"kind": "REBALANCE", "user_text": user_text[:300]})
        return Outcome(notice=("Nothing drafted", "Your message does not ask to rebalance, so no rebalance plan was drafted."))
    stated = req["max_pct"]
    if stated is None:
        return Outcome(clarify=("What is the maximum weight for a single stock? For example 'Rebalance so no single stock is more than 20% of my portfolio'.", []))
    if max_pct is not None and Decimal(str(max_pct)) != stated:
        audit.record(s, "verifier", "MISREAD_BLOCKED", "account", account.account_id, {"kind": "REBALANCE", "trader": str(stated), "model": str(max_pct)})
        return Outcome(notice=("Misread guard", f"You said {stated.normalize():f}% but the AI asked for {Decimal(str(max_pct)).normalize():f}%. Nothing was drafted; please restate the limit."))
    draft = build_rebalance_plan(s, account, extract_facts(user_text), max_pct=stated)
    return _generated_plan(s, draft, user_text=user_text, account=account, user_id=user_id, engine=engine, conversation_id=conversation_id, origin=origin, kind="REBALANCE")


def _duplicate_mandate(s: Session, user_id: str, account_id: str, mandate: CanonicalMandate):
    """An ACTIVE instruction with the same condition and action would fire twice: BLOCK and keep the existing one. One
    still awaiting a countersignature only WARNs (it may be an abandoned draft)."""
    from countersign.core.verifier import Issue

    want = (mandate.predicate.model_dump(), mandate.template.model_dump(mode="json") if mandate.template else None)
    rows = s.execute(
        select(StandingMandate).where(
            StandingMandate.user_id == user_id,
            StandingMandate.account_id == account_id,
            StandingMandate.instrument_id == mandate.instrument.instrument_id,
            StandingMandate.kind == mandate.kind,
            StandingMandate.state.in_(["ACTIVE", "PENDING_APPROVAL"]),
        )
    ).scalars().all()
    def key(t: dict | None) -> tuple:
        t = t or {}
        return t.get("side"), t.get("quantity"), t.get("limit_price_paise"), t.get("product")

    for r in sorted(rows, key=lambda r: r.state != "ACTIVE"):
        if r.predicate == want[0] and (r.template is None) == (want[1] is None) and (want[1] is None or key(r.template) == key(want[1])):
            if r.state == "ACTIVE":
                return Issue(code="DUPLICATE_MANDATE", severity="BLOCK", message=f"An identical standing instruction is already active ({describe_predicate(mandate)}). Keep the existing one: arming a second copy would make it fire twice.")
            return Issue(code="DUPLICATE_MANDATE", severity="WARN", message="An identical standing instruction is already waiting for your countersignature. Countersign only one of them, or both would fire.")
    return None


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
    if (dup := _duplicate_mandate(s, user_id, account.account_id, mandate)) is not None:
        m_issues = [dup, *m_issues]
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
        one_shot=mandate.one_shot,
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
    aggregate: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i in all_issues:
        key = (i.get("code", ""), i.get("message", ""))
        if key not in seen:
            seen.add(key)
            aggregate.append(i)
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
        "issues": aggregate,  # aggregate of plan-level and step issues (docs/API.md contract), de-duplicated
        "can_approve": can,
        "interpretation": p.interpretation,
        "approval": approval_view(approval),
        "execution": execution,
        "created_at": iso(p.created_at),
        "updated_at": iso(p.updated_at),
    }
