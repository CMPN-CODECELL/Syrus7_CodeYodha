"""Isolated executor process: the ONLY component that holds the broker write key.

Durable execution: each job runs as a DBOS workflow whose ID is derived from the job (so it executes at most once),
on a queue rate-limited to 10 orders/second (SEBI retail algo threshold). If the process crashes mid-plan, DBOS
recovers the workflow on restart and the outbox + reconciliation logic guarantees no duplicate order."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

from dbos import DBOS, DBOSConfig, Queue, SetWorkflowID
from sqlalchemy import select

from countersign.core import events
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import ExecJob
from countersign.core.state import put_state
from countersign.executor import pipeline, tracker


def _assert_isolation() -> None:
    cfg = settings()
    if not cfg.broker_write_key:
        sys.exit("executor: CS_BROKER_WRITE_KEY is required (the executor is the only process that may hold it).")
    if os.environ.get("CS_021_PASSWORD"):
        sys.exit("executor: refusing to run with the 021 password in its environment (it belongs only to gateway021).")
    forbidden = [m for m in ("pydantic_ai", "countersign.llm", "countersign.api") if m in sys.modules]
    if forbidden:
        sys.exit(f"executor: refusing to run with AI/API modules loaded: {forbidden}")


orders_queue: Queue | None = None


@DBOS.step()
def step_preflight(job_id: str) -> dict:
    return pipeline.preflight(job_id)


@DBOS.step()
def step_send(job_id: str, step: dict) -> dict:
    return pipeline.send_step(job_id, step)


@DBOS.step()
def step_wait(job_id: str, depends_on: int) -> dict:
    return pipeline.wait_for_fill(job_id, depends_on)


@DBOS.step()
def step_skip(job_id: str, from_step: int, reason: str) -> None:
    pipeline.skip_remaining(job_id, from_step, reason)


@DBOS.step()
def step_skip_one(job_id: str, step_no: int, reason: str) -> None:
    pipeline.skip_step(job_id, step_no, reason)


@DBOS.step()
def step_finalize(job_id: str) -> None:
    pipeline.finalize(job_id)


@DBOS.workflow()
def run_job(job_id: str) -> str:
    pre = step_preflight(job_id)
    if not pre.get("ok"):
        return pre.get("code", "BLOCKED")
    failed: set[int] = set()  # a failed step skips only the steps that depend on it; independent steps still run
    for step in pre["steps"]:
        dep = step.get("depends_on")
        if dep in failed:
            step_skip_one(job_id, step["step_no"], f"Step {dep} did not succeed and this step depends on it (no compensating trades).")
            failed.add(step["step_no"])
            continue
        if dep:
            w = step_wait(job_id, dep)
            if not w.get("filled"):
                step_skip_one(job_id, step["step_no"], w.get("reason", "Dependency did not fill."))
                failed.add(step["step_no"])
                continue
        r = step_send(job_id, step)
        if not r.get("ok"):
            failed.add(step["step_no"])
    step_finalize(job_id)
    return "DONE"


def _heartbeat() -> None:
    while True:
        try:
            events.client().set("cs:executor:heartbeat", str(time.time()))
            with session_scope() as s:
                put_state(s, "executor", {"pid": os.getpid(), "ts": time.time(), "holds_write_key": True})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)


def _dispatch_loop() -> None:
    seen: set[str] = set()
    wake = threading.Event()

    def _listen() -> None:
        while True:
            try:
                ps = events.client().pubsub(ignore_subscribe_messages=True)
                ps.subscribe(events.CH_EXEC_WAKE)
                for msg in ps.listen():
                    if msg.get("type") == "message":
                        wake.set()
            except Exception:  # noqa: BLE001
                time.sleep(1.0)

    threading.Thread(target=_listen, daemon=True).start()
    while True:
        try:
            with session_scope() as s:
                ids = list(s.execute(select(ExecJob.id).where(ExecJob.status == "PENDING").order_by(ExecJob.created_at)).scalars())
            for jid in ids:
                if jid in seen:
                    continue
                seen.add(jid)
                with SetWorkflowID(f"countersign-job-{jid}"):
                    assert orders_queue is not None
                    orders_queue.enqueue(run_job, jid)
        except Exception as e:  # noqa: BLE001
            print("executor dispatch error:", e)
        wake.wait(timeout=0.4)
        wake.clear()


def main() -> None:
    _assert_isolation()
    cfg = settings()
    config: DBOSConfig = {
        "name": "countersign-executor",
        "system_database_url": cfg.dbos_database_url,
        "log_level": "WARNING",
    }
    global orders_queue
    DBOS(config=config)
    DBOS.launch()
    orders_queue = DBOS.register_queue("orders", limiter={"limit": 10, "period": 1.0}, polling_interval_sec=0.2)
    tracker.start(pipeline.broker)
    threading.Thread(target=_heartbeat, daemon=True).start()
    print(json.dumps({"executor": "ready", "write_key": "held", "llm_modules_loaded": False}))
    _dispatch_loop()


if __name__ == "__main__":
    main()
