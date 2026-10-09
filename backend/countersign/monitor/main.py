"""Independent invariant monitor (separate process, outside the model's control).

It compares the broker's real order book with what was countersigned, every second. Any violation engages the
kill switch, records a critical security event and an audit entry, and is shown in the Security Center."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, text

from countersign.core import audit, events, state
from countersign.core.broker_client import BrokerAdmin, _tls
from countersign.core.canonical import canonical_hash
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import Approval, Capability, ExecJob, OutboxEntry, Plan, Proposal
from countersign.core.schemas import CanonicalOrder, CanonicalPlan

REPORTED_KEY = "cs:monitor:reported"


def watched(o: dict) -> bool:
    """Every account is watched (DEMO1, LAB1 and each provisioned PT account) except the naive copilot's NAIVE1
    sandbox, which is expected to hold unapproved orders: that contrast is the point of the Attack Lab."""
    return o.get("account_id") != settings().naive_account_id


class Monitor:
    def __init__(self, admin: BrokerAdmin | None = None) -> None:
        self.admin = admin or BrokerAdmin(settings().broker_audit_key or settings().broker_admin_key)
        self.reported: set[str] = set()
        self.last_audit_check = 0.0
        self.audit_result = {"ok": True, "checked": 0, "detail": "not yet checked"}

    def _violation(self, key: str, kind: str, message: str, data: dict) -> None:
        # Reported keys persist in Valkey so a monitor restart does not re-engage the kill switch for a violation
        # that was already recorded and deliberately released; anything new still engages it.
        if key in self.reported:
            return
        self.reported.add(key)
        if not events.client().sadd(REPORTED_KEY, key):
            return
        with session_scope() as s:
            state.security_event(s, "critical", kind, message, data, actor="monitor")
            audit.record(s, "monitor", "INVARIANT_VIOLATION", "invariant", kind, {"message": message, **data})
            if not state.kill_switch(s)["engaged"]:
                state.set_kill_switch(s, True, f"Invariant monitor: {message}", "monitor")

    def _signed_order(self, s, ob: OutboxEntry) -> CanonicalOrder | None:
        job = s.get(ExecJob, ob.job_id)
        p = s.get(Proposal, ob.proposal_id)
        if job.kind == "MANDATE":
            return CanonicalOrder.model_validate(job.order_request)
        if job.kind == "PLAN":
            plan = CanonicalPlan.model_validate(p.canonical)
            return next(st.order for st in plan.steps if st.step_no == ob.step_no)
        return CanonicalOrder.model_validate(p.canonical)

    def run_once(self) -> dict:
        checks = []
        violations = 0
        unapproved = 0
        external = 0
        try:
            orders = [o for o in self.admin.all_orders() if watched(o)]
            broker_ok = True
        except Exception as e:  # noqa: BLE001
            orders, broker_ok = [], False
            checks.append({"name": "Broker order book reachable", "ok": False, "detail": f"Cannot read broker order book: {e}", "checked": 0})

        present: set[str] | None = None

        def still_at_broker(oid: str) -> bool:
            # An order whose approval rows are gone is re-checked against a fresh order book before it is flagged:
            # a per-user demo reset deletes the broker orders first and the app rows second, so an order that has
            # since vanished was deleted, not smuggled in. If the broker cannot answer, fail closed and flag it.
            nonlocal present
            if present is None:
                try:
                    present = {o["broker_order_id"] for o in self.admin.all_orders()}
                except Exception:  # noqa: BLE001
                    return True
            return oid in present

        if broker_ok:
            bad_auth, bad_match, by_tag = [], [], {}
            with session_scope() as s:
                # Prices the trader countersigned for an order: the original plus every signed modification
                # (including one in flight), so a countersigned modify can never be mistaken for tampering.
                signed_mods: dict[str, set[int | None]] = {}
                for mob in s.execute(select(OutboxEntry)).scalars():
                    req = mob.request or {}
                    if req.get("action") == "MODIFY" and req.get("target_broker_order_id"):
                        signed_mods.setdefault(req["target_broker_order_id"], set()).add(req.get("limit_price_paise"))
                # Out-of-band orders already detected, halted and marked contained (Attack Lab drill on the LAB1
                # sandbox) are still counted as external orders but no longer hold the check red.
                contained = set(s.execute(text("SELECT data->>'broker_order_id' FROM security_events WHERE kind = 'UNAUTHORIZED_BROKER_ORDER' AND resolved AND data->>'source' = 'rogue'")).scalars())
                for o in orders:
                    tag = o.get("client_order_id")
                    if tag:
                        by_tag.setdefault(tag, []).append(o["broker_order_id"])
                    ob = s.execute(select(OutboxEntry).where(OutboxEntry.client_order_id == tag)).scalar_one_or_none() if tag else None
                    if ob is None:
                        if not still_at_broker(o["broker_order_id"]):
                            continue
                        external += 1
                        if o["broker_order_id"] in contained:
                            continue
                        bad_auth.append(o)
                        self._violation(f"ext:{o['broker_order_id']}", "UNAUTHORIZED_BROKER_ORDER", f"Broker order {o['broker_order_id']} ({o['side']} {o['quantity']} {o['symbol']}) has no countersigned approval. It did not come from the Countersign executor.", {"broker_order_id": o["broker_order_id"], "source": o.get("source"), "account_id": o.get("account_id")})
                        continue
                    cap = s.get(Capability, ob.capability_id)
                    appr = s.get(Approval, cap.approval_id) if cap else None
                    if cap is None or cap.status != "USED" or appr is None or appr.status != "VERIFIED":
                        if (cap is None or appr is None) and not still_at_broker(o["broker_order_id"]):
                            continue
                        unapproved += 1
                        bad_auth.append(o)
                        self._violation(f"auth:{o['broker_order_id']}", "UNAPPROVED_EXECUTION", f"Order {o['broker_order_id']} was sent without a consumed, verified capability.", {"broker_order_id": o["broker_order_id"], "account_id": o.get("account_id")})
                        continue
                    try:
                        signed = self._signed_order(s, ob)
                    except Exception:
                        if not still_at_broker(o["broker_order_id"]):
                            continue
                        raise
                    allowed_limits = {signed.limit_price_paise} | signed_mods.get(o["broker_order_id"], set())
                    fields_ok = (
                        signed.instrument.instrument_id == o["instrument_id"]
                        and signed.side.value == o["side"]
                        and (signed.action != "PLACE" or signed.quantity == o["quantity"])
                        and (signed.action != "PLACE" or (o.get("limit_price_paise") or None) in {(x or None) for x in allowed_limits})
                    )
                    if not fields_ok:
                        unapproved += 1
                        bad_match.append(o)
                        self._violation(f"match:{o['broker_order_id']}", "ORDER_DIFFERS_FROM_SIGNED", f"Broker order {o['broker_order_id']} differs from the countersigned order.", {"broker_order_id": o["broker_order_id"], "account_id": o.get("account_id")})
                drills = len(contained & {o["broker_order_id"] for o in orders})
                auth_ok = "All orders map to a consumed, verified capability." + (f" {drills} Attack Lab drill order(s) detected and contained." if drills else "")
                checks.append({"name": "Every broker order carries a valid countersignature", "ok": not bad_auth, "detail": auth_ok if not bad_auth else f"{len(bad_auth)} order(s) without valid authorization.", "checked": len(orders)})
                checks.append({"name": "Broker orders match the signed canonical order", "ok": not bad_match, "detail": "Instrument, side, quantity and price match what was signed." if not bad_match else f"{len(bad_match)} mismatch(es).", "checked": len(orders)})
                dups = {t: ids for t, ids in by_tag.items() if len(ids) > 1}
                for t, ids in dups.items():
                    self._violation(f"dup:{t}", "DUPLICATE_CLIENT_ORDER_ID", f"Broker holds {len(ids)} orders with client tag {t}.", {"tag": t, "orders": ids})
                checks.append({"name": "One broker order per client order id", "ok": not dups, "detail": "No duplicate tags at the broker." if not dups else f"{len(dups)} duplicated tag(s).", "checked": len(by_tag)})

                reuse = s.execute(text("SELECT capability_id, count(DISTINCT job_id) n FROM outbox GROUP BY capability_id HAVING count(DISTINCT job_id) > 1")).all()
                for cap_id, n in reuse:
                    self._violation(f"reuse:{cap_id}", "CAPABILITY_REUSE", f"Capability {cap_id} was used by {n} jobs.", {"capability_id": cap_id})
                n_caps = s.execute(text("SELECT count(*) FROM capabilities")).scalar()
                checks.append({"name": "Capabilities are single-use", "ok": not reuse, "detail": "No capability backs more than one execution." if not reuse else "Reuse detected.", "checked": n_caps})

                multi = s.execute(text("SELECT j.mandate_id, count(*) FROM outbox o JOIN exec_jobs j ON j.id = o.job_id WHERE j.kind='MANDATE' GROUP BY j.mandate_id HAVING count(*) > 1")).all()
                for mid, n in multi:
                    self._violation(f"mandate:{mid}", "MANDATE_FIRED_TWICE", f"Standing instruction {mid} produced {n} orders.", {"mandate_id": mid})
                n_m = s.execute(text("SELECT count(*) FROM mandates WHERE state IN ('FIRED','COMPLETED','EXECUTING','TRIGGERED')")).scalar()
                checks.append({"name": "Standing instructions fire at most once", "ok": not multi, "detail": "Every triggered mandate produced at most one order." if not multi else "Multiple fires detected.", "checked": n_m})

                late = []
                for job, appr in s.execute(select(ExecJob, Approval).join(Capability, Capability.id == ExecJob.capability_id).join(Approval, Approval.id == Capability.approval_id).where(ExecJob.kind != "MANDATE", ExecJob.started_at.is_not(None), ExecJob.status.in_(["RUNNING", "DONE"]))).all():
                    if job.started_at > appr.expires_at + timedelta(seconds=2):
                        late.append(job.id)
                        self._violation(f"late:{job.id}", "EXECUTION_AFTER_EXPIRY", f"Job {job.id} started after its approval expired.", {"job_id": job.id})
                checks.append({"name": "No execution after approval expiry", "ok": not late, "detail": "Every execution started inside its 60 s approval window." if not late else f"{len(late)} late execution(s).", "checked": s.execute(text("SELECT count(*) FROM exec_jobs WHERE started_at IS NOT NULL")).scalar()})

                changed = []
                for plan, prop in s.execute(select(Plan, Proposal).join(Proposal, Proposal.id == Plan.id).where(Plan.state.notin_(["AWAITING_APPROVAL", "BLOCKED", "REJECTED"]))).all():
                    if plan.plan_hash != canonical_hash(prop.canonical) or prop.canonical_hash != plan.plan_hash:
                        changed.append(plan.id)
                        self._violation(f"plan:{plan.id}", "PLAN_MODIFIED_AFTER_APPROVAL", f"Signed plan {plan.id} no longer matches its hash.", {"plan_id": plan.id})
                checks.append({"name": "Signed plans are immutable", "ok": not changed, "detail": "All approved plans match their signed hash." if not changed else f"{len(changed)} modified plan(s).", "checked": s.execute(text("SELECT count(*) FROM plans")).scalar()})

        if time.time() - self.last_audit_check > 10:
            self.last_audit_check = time.time()
            with session_scope() as s:
                self.audit_result = audit.verify_chain(s)
            if not self.audit_result["ok"]:
                self._violation(f"audit:{self.audit_result['broken_at_id']}", "AUDIT_CHAIN_BROKEN", self.audit_result["detail"], {"broken_at_id": self.audit_result["broken_at_id"]})
        checks.append({"name": "Audit chain intact", "ok": self.audit_result["ok"], "detail": self.audit_result["detail"], "checked": self.audit_result.get("checked", 0)})

        try:
            h = httpx.get("http://127.0.0.1:8000/api/health", timeout=2.0, verify=_tls()).json()
            api_ok = h.get("holds_write_key") is False
            checks.append({"name": "API / AI process holds no broker write key", "ok": api_ok, "detail": "The API process reports no write credential." if api_ok else "API process reports a write key!", "checked": 1})
            if not api_ok:
                self._violation("api-key", "CREDENTIAL_ZONE_VIOLATION", "The API process holds a broker write key.", {})
        except Exception:  # noqa: BLE001
            checks.append({"name": "API / AI process holds no broker write key", "ok": True, "detail": "API not reachable right now (it cannot trade either way).", "checked": 0})

        violations = sum(1 for c in checks if not c["ok"])
        status = {
            "healthy": True,
            "last_heartbeat": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "checks": checks,
            "violations": violations,
            "unapproved_executions": unapproved,
            "external_orders_detected": external,
            "_ts": time.time(),
        }
        events.client().set("cs:monitor:status", json.dumps(status))
        return status


def main() -> None:
    m = Monitor()
    print(json.dumps({"monitor": "ready"}))
    while True:
        try:
            m.run_once()
        except Exception as e:  # noqa: BLE001
            print("monitor error:", e)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
