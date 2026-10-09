"""Standing-mandate engine. Evaluates ACTIVE mandates on every live tick.

One-shot firing is enforced by an atomic compare-and-set (ACTIVE -> TRIGGERED with a unique trigger_event_id), so
two ticks, two engine instances or a restart can never fire the same mandate twice. If the condition became true
while the system was offline (gap since the last evaluation), it moves to NEEDS_REVIEW instead of firing stale.

Alerts over every holding ("tell me when any holding falls 3% in a day") stay ACTIVE and fire at most once per
holding per IST trading day: each firing first inserts a durable dedupe row keyed by (mandate, instrument, day) with
INSERT ... ON CONFLICT DO NOTHING, so concurrent engines and restarts can never fire the same holding twice a day."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from countersign.core import audit, events, state
from countersign.core.broker_client import ReadGateway, read_gateway
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.market import IST, Tick
from countersign.core.models import ExecJob, StandingMandate
from countersign.core.money import inr
from countersign.core.schemas import ALL_HOLDINGS_ID, CanonicalOrder
from countersign.core.verifier import AccountState, verify_order


def predicate_true(m: StandingMandate, tick: Tick, avg_price: int | None) -> bool:
    p = m.predicate
    if p["field"] == "LTP":
        return tick.ltp_paise <= p["threshold"] if p["op"] == "LTE" else tick.ltp_paise >= p["threshold"]
    if p["field"] == "DROP_FROM_AVG_BPS" and avg_price:
        drop_bps = (avg_price - tick.ltp_paise) * 10000 // avg_price
        return drop_bps >= p["threshold"]
    if p["field"] == "DAY_CHANGE_BPS" and tick.prev_close_paise:
        return (tick.ltp_paise - tick.prev_close_paise) * 10000 <= p["threshold"] * tick.prev_close_paise
    return False


def evaluate(
    s: Session, m: StandingMandate, tick: Tick, *, gateway: ReadGateway | None = None, engine_id: str = "engine-1", held: dict[str, dict] | None = None
) -> str:
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
    if m.predicate["field"] == "DROP_FROM_AVG_BPS" and (held is not None or gateway is not None):
        h = (held if held is not None else {x["instrument_id"]: x for x in gateway.holdings()}).get(m.instrument_id)
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


HOLDINGS_REFRESH_S = 20.0  # holdings are re-read from the broker at most this often per account (rate limits)


def trading_day(tick: Tick) -> date:
    return tick.ts.astimezone(IST).date()


def fire_key(mandate_id: str, instrument_id: str, day: date) -> str:
    """Durable dedupe key for one holdings-alert firing (fits system_state.key, 64 chars)."""
    return f"af:{mandate_id}:{day:%Y%m%d}:{hashlib.sha256(instrument_id.encode()).hexdigest()[:12]}"


def holdings_alert_hit(predicate: dict, tick: Tick, holding: dict) -> tuple[bool, str]:
    """Exact integer comparison (no rounding): DAY_CHANGE_BPS LTE -300 means (ltp - prev) / prev <= -3%."""
    ltp = tick.ltp_paise
    if predicate["field"] == "DAY_CHANGE_BPS":
        prev = int(holding.get("prev_close_paise") or 0) or tick.prev_close_paise
        if not prev or prev <= 0:
            return False, ""
        hit = (ltp - prev) * 10000 <= predicate["threshold"] * prev
        return hit, f"down {(prev - ltp) * 100 / prev:.2f}% today ({inr(ltp)} against the previous close {inr(prev)})"
    if predicate["field"] == "DROP_FROM_AVG_BPS":
        avg = int(holding.get("avg_price_paise") or 0)
        if avg <= 0:
            return False, ""
        hit = (avg - ltp) * 10000 >= predicate["threshold"] * avg
        return hit, f"{(avg - ltp) * 100 / avg:.2f}% below your average price ({inr(ltp)} against {inr(avg)})"
    return False, ""


def evaluate_holdings_alert(s: Session, m: StandingMandate, tick: Tick, held: dict[str, dict] | None, *, engine_id: str = "engine-1") -> str:
    """Returns NONE | EXPIRED | TRIGGERED | DUPLICATE. Never places an order."""
    if m.state != "ACTIVE" or m.instrument_id != ALL_HOLDINGS_ID or m.kind != "ALERT":
        return "NONE"
    if datetime.now(UTC) >= m.expires_at:
        s.execute(text("UPDATE mandates SET state='EXPIRED', reason='Expired' WHERE id=:id AND state='ACTIVE'"), {"id": m.id})
        audit.record(s, "mandate_engine", "MANDATE_EXPIRED", "mandate", m.id, {})
        events.emit_mandate(m, "EXPIRED")
        return "EXPIRED"
    if tick.stale or not held:
        return "NONE"
    h = held.get(tick.instrument_id)
    if h is None or int(h.get("quantity") or 0) <= 0:
        return "NONE"
    hit, detail = holdings_alert_hit(m.predicate, tick, h)
    if not hit:
        return "NONE"
    day = trading_day(tick)
    event_id = f"{m.id[:8]}:{tick.instrument_id}:{day:%Y%m%d}"
    won = s.execute(
        text("INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), now()) ON CONFLICT (key) DO NOTHING RETURNING key"),
        {
            "k": fire_key(m.id, tick.instrument_id, day),
            "v": json.dumps({"mandate_id": m.id, "instrument_id": tick.instrument_id, "trading_day": day.isoformat(), "event_id": event_id, "ltp_paise": tick.ltp_paise, "engine": engine_id}),
        },
    ).first()
    if not won:
        return "DUPLICATE"
    s.execute(text("UPDATE mandates SET triggered_at=now(), fired_at=now(), last_evaluated_at=:ts WHERE id=:id"), {"id": m.id, "ts": tick.ts})
    audit.record(s, "mandate_engine", "ALERT_FIRED", "mandate", m.id, {"event_id": event_id, "instrument_id": tick.instrument_id, "ltp_paise": tick.ltp_paise, "trading_day": day.isoformat(), "engine": engine_id})
    state.outcome(
        s,
        "ALERT",
        f"Alert: {tick.symbol} {detail.split(' (')[0]}",
        f"{tick.symbol} is {detail}. This alert fires at most once per holding per trading day (event {event_id}). No order was placed.",
        "warning",
        proposal_id=m.proposal_id,
        account_id=m.account_id,
    )
    events.emit_mandate(m, "ACTIVE")
    return "TRIGGERED"


class Engine:
    def __init__(self, engine_id: str = "engine-1"):
        self.engine_id = engine_id
        self._active: dict[str, list[str]] = {}
        self._holdings_alerts: list[tuple[str, str, dict]] = []  # (mandate id, account id, predicate)
        self._last_refresh = 0.0
        self._gws: dict[str, ReadGateway] = {}
        self._held: dict[str, tuple[float, dict[str, dict]]] = {}
        self._done: set[str] = set()  # dedupe keys already settled in this process (saves a DB round trip per tick)

    def gateway(self, account_id: str) -> ReadGateway | None:
        if account_id not in self._gws and settings().broker_read_key:
            self._gws[account_id] = read_gateway(account_id)
        return self._gws.get(account_id)

    def holdings_for(self, account_id: str) -> dict[str, dict] | None:
        cached = self._held.get(account_id)
        if cached is not None and time.time() - cached[0] < HOLDINGS_REFRESH_S:
            return cached[1]
        gw = self.gateway(account_id)
        if gw is None:
            return cached[1] if cached else None
        try:
            rows = {h["instrument_id"]: h for h in gw.holdings()}
        except Exception:  # noqa: BLE001
            return cached[1] if cached else None
        self._held[account_id] = (time.time(), rows)
        return rows

    def refresh(self) -> None:
        with session_scope() as s:
            rows = s.execute(
                select(StandingMandate.id, StandingMandate.instrument_id, StandingMandate.account_id, StandingMandate.predicate).where(StandingMandate.state == "ACTIVE")
            ).all()
        idx: dict[str, list[str]] = {}
        alerts: list[tuple[str, str, dict]] = []
        for mid, iid, acct, pred in rows:
            if iid == ALL_HOLDINGS_ID:
                alerts.append((mid, acct, pred))
            else:
                idx.setdefault(iid, []).append(mid)
        self._active = idx
        self._holdings_alerts = alerts
        self._last_refresh = time.time()

    def on_tick(self, tick: Tick) -> None:
        if time.time() - self._last_refresh > 1.0:
            self.refresh()
        for mid in self._active.get(tick.instrument_id, []):
            try:
                with session_scope() as s:
                    m = s.get(StandingMandate, mid)
                    if m is not None:
                        held = self.holdings_for(m.account_id) if m.predicate.get("field") == "DROP_FROM_AVG_BPS" else None
                        evaluate(s, m, tick, gateway=self.gateway(m.account_id), engine_id=self.engine_id, held=held)
            except Exception as e:  # noqa: BLE001
                print("mandate engine error:", e)
        for mid, acct, pred in self._holdings_alerts:
            try:
                held = self.holdings_for(acct)
                h = (held or {}).get(tick.instrument_id)
                if h is None or tick.stale or not holdings_alert_hit(pred, tick, h)[0]:
                    continue
                key = fire_key(mid, tick.instrument_id, trading_day(tick))
                if key in self._done:
                    continue
                with session_scope() as s:
                    m = s.get(StandingMandate, mid)
                    if m is not None and evaluate_holdings_alert(s, m, tick, held, engine_id=self.engine_id) in ("TRIGGERED", "DUPLICATE"):
                        self._done.add(key)
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
