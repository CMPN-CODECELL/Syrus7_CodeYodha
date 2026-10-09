from __future__ import annotations

import json
import time
import traceback
from datetime import UTC, datetime

from sqlalchemy import select

from countersign.attack_lab.scenarios import SCENARIOS, Lab
from countersign.core import audit, events
from countersign.core.broker_client import BrokerAdmin
from countersign.core.db import session_scope
from countersign.core.models import AttackRunRow
from countersign.core.schemas import iso


def _naive_total() -> int:
    try:
        return sum(1 for o in BrokerAdmin().all_orders() if o.get("account_id") == "NAIVE1")
    except Exception:  # noqa: BLE001
        return 0


def scoreboard() -> dict:
    raw = events.client().get("cs:monitor:status")
    mon = json.loads(raw) if raw else {}
    with session_scope() as s:
        last = s.execute(select(AttackRunRow).order_by(AttackRunRow.started_at.desc()).limit(1)).scalar_one_or_none()
        runs = s.execute(select(AttackRunRow)).scalars().all()
    attempted = sum(r.scoreboard.get("run_attempted", 0) for r in runs)
    blocked = sum(r.scoreboard.get("run_blocked", 0) for r in runs)
    return {
        "unapproved_executions": int(mon.get("unapproved_executions", 0)),
        "naive_unapproved_executions": _naive_total(),
        "attacks_attempted": attempted,
        "attacks_blocked": blocked,
        "last_run_at": iso(last.started_at) if last else None,
    }


def run(scenario_id: str = "all") -> dict:
    chosen = SCENARIOS if scenario_id == "all" else [s for s in SCENARIOS if s.id == scenario_id or s.letter.lower() == scenario_id.lower()]
    # Every run starts from a funded sandbox, so a legitimate setup order is never blocked by an earlier run's spending.
    BrokerAdmin().replenish("LAB1")
    started = datetime.now(UTC)
    results = []
    tot_att = tot_blk = 0
    for sc in chosen:
        lab = Lab()
        t0 = time.time()
        naive_before = _naive_total()
        try:
            lab.snapshot()
            verdict = sc.fn(lab)
            unapproved = lab.unapproved()
            status = "BLOCKED" if unapproved == 0 and not any(l["outcome"] == "allowed" and l["actor"] != "naive-copilot" for l in lab.lines) else "FAILED_OPEN"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            lab.log("attacker", f"Scenario error: {type(e).__name__}: {e}", "info")
            verdict, unapproved, status = f"Scenario could not complete: {e}", lab.unapproved() if lab.lab_orders_before else 0, "ERROR"
        naive_new = max(0, _naive_total() - naive_before)
        results.append(
            {
                "scenario_id": sc.id,
                "letter": sc.letter,
                "title": sc.title,
                "status": status,
                "guarantee": sc.guarantee,
                "verdict": verdict,
                "attempted_actions": lab.attempted,
                "blocked_actions": lab.blocked,
                "unapproved_orders": unapproved,
                "naive_unapproved_orders": naive_new,
                "transcript": lab.lines,
                "duration_ms": int((time.time() - t0) * 1000),
            }
        )
    finished = datetime.now(UTC)
    sb = scoreboard()
    tot_att = len(results)
    tot_blk = sum(1 for r in results if r["status"] == "BLOCKED")
    sb_run = {**sb, "run_attempted": tot_att, "run_blocked": tot_blk}
    with session_scope() as s:
        row = AttackRunRow(started_at=started, finished_at=finished, results=results, scoreboard=sb_run)
        s.add(row)
        s.flush()
        audit.record(s, "attack_lab", "ATTACK_LAB_RUN", "attack_run", row.id, {"scenarios": [r["scenario_id"] for r in results], "statuses": [r["status"] for r in results], "unapproved_orders": sum(r["unapproved_orders"] for r in results)})
        rid = row.id
    sb = scoreboard()
    return {"id": rid, "started_at": iso(started), "finished_at": iso(finished), "results": results, "scoreboard": sb}
