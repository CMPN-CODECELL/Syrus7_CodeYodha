"""Exercises the advanced demo flows over HTTP: standing instruction + tick replay trigger, plan with partial fill,
and modify/cancel. Countersigns with the PIN fallback (the browser demo uses the passkey)."""

from __future__ import annotations

import sys
import time

from smoke import Client, proposal_from

PIN = "2580"


def approve(c: Client, pid: str) -> dict:
    st, start = c.post(f"/api/proposals/{pid}/approval/start", {"method": "pin"})
    assert st == 200, start
    st, done = c.post(f"/api/proposals/{pid}/approval/complete", {"approval_id": start["approval_id"], "pin": PIN})
    assert st == 200, done
    return done


def wait_status(c: Client, pid: str, targets: set[str], timeout: float = 25) -> dict:
    end = time.time() + timeout
    p = {}
    while time.time() < end:
        _, p = c.get(f"/api/proposals/{pid}")
        if p["status"] in targets:
            return p
        time.sleep(0.5)
    return p


def draft(c: Client, text: str) -> dict:
    ev = c.chat(text, engine="deterministic")
    pid = proposal_from(ev)
    assert pid, [e for e in ev if e["type"] == "card"]
    _, p = c.get(f"/api/proposals/{pid}")
    print(f"  draft: {p['kind']} {p['status']} :: {p['summary']}")
    return p


def main() -> int:
    c = Client()
    c.post("/api/demo/reset")
    time.sleep(1.5)
    c.post("/api/auth/pin", {"pin": PIN})

    print("1. Standing instruction + dip_to_trigger replay")
    p = draft(c, "Buy 100 ABCCORP if the price falls below 500")
    approve(c, p["id"])
    _, ms = c.get("/api/mandates")
    print("  mandate:", ms[0]["state"], ms[0]["condition_text"])
    c.post("/api/demo/feed", {"scenario": "dip_to_trigger", "symbol": "ABCCORP"})
    end = time.time() + 20
    while time.time() < end:
        _, ms = c.get("/api/mandates")
        if ms[0]["state"] in ("FIRED", "COMPLETED", "FAILED", "NEEDS_REVIEW"):
            break
        time.sleep(0.5)
    time.sleep(2)
    _, ms = c.get("/api/mandates")
    print("  mandate after replay:", ms[0]["state"], ms[0]["trigger_event_id"], ms[0].get("reason"))
    _, orders = c.get("/api/account/orders?status=all")
    print("  ABCCORP orders at broker:", [(o["side"], o["quantity"], o["status"]) for o in orders if o["instrument"]["symbol"] == "ABCCORP"])

    print("2. Plan with partial fill on step 1")
    _, q = c.get("/api/market/quote/INFY")
    sell_px = (q["tick"]["ltp_paise"] - 300) // 5 * 5
    _, q2 = c.get("/api/market/quote/TCS")
    buy_px = (q2["tick"]["ltp_paise"] + 400) // 10 * 10
    p = draft(c, f"sell 20 INFY at {sell_px / 100:.2f} and buy 2 TCS at {buy_px / 100:.2f}")
    c.post("/api/demo/broker-fault", {"mode": "partial_fill", "count": 1})
    approve(c, p["id"])
    p = wait_status(c, p["id"], {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED"}, timeout=40)
    print("  plan:", p["status"], [(s["step_no"], s.get("state"), s.get("filled_quantity"), (s.get("reason") or "")[:70]) for s in p["steps"]])

    print("3. Resting order -> modify -> cancel")
    _, q = c.get("/api/market/quote/INFY")
    low = (q["tick"]["ltp_paise"] * 97 // 100) // 5 * 5
    p = draft(c, f"buy 3 INFY at {low / 100:.2f}")
    approve(c, p["id"])
    p = wait_status(c, p["id"], {"EXECUTING", "COMPLETED", "FAILED"})
    print("  placed:", p["status"], p["steps"][0].get("state"), p["steps"][0].get("broker_order_id"))
    p2 = draft(c, f"modify my INFY order to {(low - 500) / 100:.2f}")
    approve(c, p2["id"])
    p2 = wait_status(c, p2["id"], {"COMPLETED", "FAILED"})
    print("  modify:", p2["status"])
    p3 = draft(c, "cancel my INFY order")
    approve(c, p3["id"])
    p3 = wait_status(c, p3["id"], {"COMPLETED", "FAILED"})
    print("  cancel:", p3["status"])
    _, orders = c.get("/api/account/orders?status=all")
    print("  INFY orders:", [(o["side"], o["quantity"], o["limit_price_paise"], o["status"]) for o in orders if o["instrument"]["symbol"] == "INFY"])

    _, sec = c.get("/api/security/status")
    print("monitor violations:", sec["monitor"]["violations"], "unapproved:", sec["monitor"]["unapproved_executions"])
    _, av = c.get("/api/audit/verify")
    print("audit:", av["detail"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
