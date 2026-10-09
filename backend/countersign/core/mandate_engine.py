"""Standing-mandate engine. Evaluates ACTIVE mandates on every live tick.

One-shot firing is enforced by an atomic compare-and-set (ACTIVE -> TRIGGERED with a unique trigger_event_id), so
two ticks, two engine instances or a restart can never fire the same mandate twice. If the condition became true
while the system was offline (gap since the last evaluation), it moves to NEEDS_REVIEW instead of firing stale."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from countersign.core import audit, events, state
from countersign.core.broker_client import ReadGateway
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.market import Tick
from countersign.core.models import ExecJob, StandingMandate
from countersign.core.money import inr
from countersign.core.schemas import CanonicalOrder
from countersign.core.verifier import AccountState, verify_order


def predicate_true(m: StandingMandate, tick: Tick, avg_price: int | None) -> bool:
    p = m.predicate
    if p["field"] == "LTP":
        return tick.ltp_paise <= p["threshold"] if p["op"] == "LTE" else tick.ltp_paise >= p["threshold"]
    if p["field"] == "DROP_FROM_AVG_BPS" and avg_price:
        drop_bps = (avg_price - tick.ltp_paise) * 10000 // avg_price
        return drop_bps >= p["threshold"]
    return False


def evaluate(s: Session, m: StandingMandate, tick: Tick, *, gateway: ReadGateway | None = None, engine_id: str = "engine-1") -> str:
    """Returns what happened: NONE | EXPIRED | NEEDS_REVIEW | TRIGGERED | LOST_RACE | FAILED."""
    now = datetime.now(UTC)
    if m.state != "ACTIVE":
        return "NONE"
    if now >= m.expires_at:
        s.execute(text("UPDATE mandates SET state='EXPIRED', reason='Expired without triggering' WHERE id=:id AND state='ACTIVE'"), {"id": m.id})
        audit.record(s, "mandate_engine", "MANDATE_EXPIRED", "mandate", m.id, {})
        return "EXPIRED"
    if tick.stale:
        return "NONE"
    avg = None
    if m.predicate["field"] == "DROP_FROM_AVG_BPS" and gateway is not None:
        h = {x["instrument_id"]: x for x in gateway.holdings()}.get(m.instrument_id)
        avg = int(h["avg_price_paise"]) if h else None
    hit = predicate_true(m, tick, avg)
    gap = settings().mandate_gap_seconds
    stale_gap = m.last_evaluated_at is not None and (tick.ts - m.last_evaluated_at) > timedelta(seconds=gap)
    if not hit:
        s.execute(text("UPDATE mandates SET last_evaluated_at=:ts WHERE id=:id AND state='ACTIVE'"), {"id": m.id, "ts": tick.ts})
        return "NONE"
    if stale_gap:
        r = s.execute(
            text("UPDATE mandates SET state='NEEDS_REVIEW', reason=:r WHERE id=:id AND state='ACTIVE' RETURNING id"),
            {"id": m.id, "r": f"The condition became true while the system was offline (no evaluation for {int((tick.ts - m.last_evaluated_at).total_seconds())}s). It will not fire stale; review and countersign again."},
        ).first()
        if r:
            audit.record(s, "mandate_engine", "MANDATE_NEEDS_REVIEW", "mandate", m.id, {"ltp_paise": tick.ltp_paise, "gap_s": (tick.ts - m.last_evaluated_at).total_seconds()})
            state.outcome(s, "MANDATE_NEEDS_REVIEW", "Standing instruction paused for review", f"{tick.symbol} crossed the trigger while the system was offline. Not fired; please review.", "warning", proposal_id=m.proposal_id, account_id=m.account_id)
            events.emit_mandate(m, "NEEDS_REVIEW")
        return "NEEDS_REVIEW"
    event_id = f"{m.id[:8]}:{tick.instrument_id}:{tick.seq}"
    won = s.execute(
        text(
            "UPDATE mandates SET state='TRIGGERED', triggered_at=now(), trigger_event_id=:eid, trigger_tick=CAST(:tick AS jsonb), last_evaluated_at=:ts "
            "WHERE id=:id AND state='ACTIVE' RETURNING id"
        ),
        {"id": m.id, "eid": event_id, "tick": json.dumps(tick.to_api()), "ts": tick.ts},
    ).first()
    if not won:
        return "LOST_RACE"
    audit.record(s, "mandate_engine", "MANDATE_TRIGGERED", "mandate", m.id, {"trigger_event_id": event_id, "ltp_paise": tick.ltp_paise, "engine": engine_id})
    if m.kind == "ALERT":
        s.execute(text("UPDATE mandates SET state='COMPLETED', fired_at=now() WHERE id=:id"), {"id": m.id})
        state.outcome(s, "ALERT", f"Alert: {tick.symbol} at {inr(tick.ltp_paise)}", f"Your standing alert condition was met (event {event_id}). No order was placed.", "warning", proposal_id=m.proposal_id, account_id=m.account_id)
        events.emit_mandate(m, "COMPLETED")
        return "TRIGGERED"
    order = CanonicalOrder.model_validate(m.template)
    issues: list = []
    if gateway is not None:
        try:
            acct = AccountState.load(gateway, m.account_id)
            issues, _ = verify_order(s, order, acct)
        except Exception as e:  # noqa: BLE001
            issues = [type("I", (), {"severity": "BLOCK", "message": f"Could not revalidate account state: {e}"})()]
    blocking = [i for i in issues if getattr(i, "severity", "") == "BLOCK"]
    if blocking:
        msg = "; ".join(i.message for i in blocking)
        s.execute(text("UPDATE mandates SET state='FAILED', reason=:r WHERE id=:id"), {"id": m.id, "r": f"Revalidation failed at trigger time: {msg}"})
        audit.record(s, "mandate_engine", "MANDATE_REVALIDATION_FAILED", "mandate", m.id, {"issues": msg})
        state.outcome(s, "EXECUTION_BLOCKED", "Standing instruction not executed", f"Condition met but revalidation failed: {msg}", "warning", proposal_id=m.proposal_id, account_id=m.account_id)
        return "FAILED"
    job = ExecJob(capability_id=m.capability_id, kind="MANDATE", proposal_id=m.proposal_id, mandate_id=m.id, order_request=m.template, status="PENDING", source="mandate")
    s.add(job)
    s.flush()
    audit.record(s, "mandate_engine", "EXECUTION_REQUESTED", "job", job.id, {"mandate_id": m.id, "trigger_event_id": event_id})
    state.outcome(s, "MANDATE_TRIGGERED", f"Standing instruction triggered: {tick.symbol} at {inr(tick.ltp_paise)}", f"Firing the signed order once: {order.side.value} {order.quantity} {order.instrument.symbol} @ {inr(order.limit_price_paise)} LIMIT.", "info", proposal_id=m.proposal_id, account_id=m.account_id)
    events.emit_mandate(m, "TRIGGERED")
    events.publish(events.CH_EXEC_WAKE, {"mandate_id": m.id})
    return "TRIGGERED"


class Engine:
    def __init__(self, engine_id: str = "engine-1"):
        self.engine_id = engine_id
        self._active: dict[str, list[str]] = {}
        self._last_refresh = 0.0
        self._gws: dict[str, ReadGateway] = {}

    def gateway(self, account_id: str) -> ReadGateway | None:
        if account_id not in self._gws and settings().broker_read_key:
            self._gws[account_id] = ReadGateway(account_id=account_id)
        return self._gws.get(account_id)

    def refresh(self) -> None:
        with session_scope() as s:
            rows = s.execute(select(StandingMandate.id, StandingMandate.instrument_id).where(StandingMandate.state == "ACTIVE")).all()
        idx: dict[str, list[str]] = {}
        for mid, iid in rows:
            idx.setdefault(iid, []).append(mid)
        self._active = idx
        self._last_refresh = time.time()

    def on_tick(self, tick: Tick) -> None:
        if time.time() - self._last_refresh > 1.0:
            self.refresh()
        for mid in self._active.get(tick.instrument_id, []):
            try:
                with session_scope() as s:
                    m = s.get(StandingMandate, mid)
                    if m is not None:
                        evaluate(s, m, tick, gateway=self.gateway(m.account_id), engine_id=self.engine_id)
            except Exception as e:  # noqa: BLE001
                print("mandate engine error:", e)

    def run_forever(self) -> None:
        while True:
            try:
                ps = events.client().pubsub(ignore_subscribe_messages=True)
                ps.subscribe(events.CH_TICKS)
                for msg in ps.listen():
                    if msg.get("type") == "message":
                        self.on_tick(Tick.from_json(msg["data"]))
            except Exception:  # noqa: BLE001
                time.sleep(1.0)


def start_background() -> Engine:
    eng = Engine()
    threading.Thread(target=eng.run_forever, daemon=True, name="mandate-engine").start()
    return eng
