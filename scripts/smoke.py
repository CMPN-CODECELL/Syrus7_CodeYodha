"""End-to-end smoke test against a running stack (scripts/dev.py). Uses the PIN fallback for countersigning."""

from __future__ import annotations

import json
import sys
import time

import httpx

API = "http://127.0.0.1:8000"

# Answers carry the rupee sign; a Windows console defaults to cp1252, which cannot print it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class Client:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=API, timeout=60, headers={"Origin": "http://localhost:3000"})
        r = self.c.post("/api/auth/demo-login", json={})
        r.raise_for_status()
        self.csrf = self.c.cookies.get("cs_csrf")

    def get(self, path: str, **kw):
        r = self.c.get(path, **kw)
        return r.status_code, r.json()

    def post(self, path: str, body: dict | None = None):
        r = self.c.post(path, json=body or {}, headers={"X-CSRF-Token": self.csrf})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    def chat(self, message: str, engine: str = "deterministic") -> list[dict]:
        out = []
        with self.c.stream("POST", "/api/chat", json={"message": message, "engine": engine}, headers={"X-CSRF-Token": self.csrf}) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    out.append(json.loads(line[6:]))
        return out


def proposal_from(events: list[dict]) -> str | None:
    for e in events:
        if e.get("type") == "card" and e["card"]["kind"] == "proposal":
            return e["card"]["proposal_id"]
    return None


def main() -> int:
    c = Client()
    print("summary:", c.get("/api/account/summary")[1]["day_pnl_paise"])
    ev = c.chat("What is my P&L today?")
    print("pnl chat:", "".join(e.get("delta", "") for e in ev if e["type"] == "text-delta"))
    ev = c.chat("Which positions are down more than 5%?")
    print("losers:", "".join(e.get("delta", "") for e in ev if e["type"] == "text-delta"))
    ev = c.chat("What is my average purchase price for Reliance?")
    print("avg:", "".join(e.get("delta", "") for e in ev if e["type"] == "text-delta"))
    ev = c.chat("Show NIFTY options near the money")
    print("chain:", "".join(e.get("delta", "") for e in ev if e["type"] == "text-delta"))

    _, q = c.get("/api/market/quote/INFY")
    ltp = q["tick"]["ltp_paise"]
    price = (ltp + 400) // 5 * 5
    msg = f"buy 5 INFY at {price / 100:.2f}"
    ev = c.chat(msg)
    pid = proposal_from(ev)
    _, p = c.get(f"/api/proposals/{pid}")
    print("proposal:", p["status"], p["summary"], [i["code"] for i in p["steps"][0]["issues"]])
    st, _ = c.post("/api/auth/pin", {"pin": "2580"})
    if st != 200:
        c.post("/api/auth/pin", {"pin": "2580", "current_pin": "2580"})
    st, start = c.post(f"/api/proposals/{pid}/approval/start", {"method": "pin"})
    print("approval/start:", st, start.get("payload_hash", start)[:16] if isinstance(start, dict) else start)
    st, done = c.post(f"/api/proposals/{pid}/approval/complete", {"approval_id": start["approval_id"], "pin": "2580"})
    print("approval/complete:", st, done.get("status"), done.get("message"))
    for _ in range(20):
        _, p = c.get(f"/api/proposals/{pid}")
        if p["status"] in ("COMPLETED", "FAILED", "EXPIRED"):
            break
        time.sleep(0.5)
    print("final:", p["status"], p["execution"], p["steps"][0].get("state"), p["steps"][0].get("broker_order_id"))
    _, outs = c.get("/api/outcomes")
    for o in outs[:6]:
        print("  outcome:", o["kind"], "|", o["title"])
    _, sec = c.get("/api/security/status")
    print("monitor:", sec["monitor"].get("violations"), "unapproved:", sec["monitor"].get("unapproved_executions"), "executor:", sec["executor"]["healthy"], "llm:", sec["llm"])
    _, av = c.get("/api/audit/verify")
    print("audit:", av["detail"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
