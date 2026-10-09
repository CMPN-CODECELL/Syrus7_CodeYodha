"""Opt-in live check of the 021 sandbox THROUGH the gateway021 process (its broker keys; the gateway alone holds the
021 credentials). Runs only when the gateway is up; never logs in to 021 itself (one live token per account).

Steps 3 to 7 send orders with the write key directly, outside the executor. On a full stack the invariant monitor
therefore flags them as unauthorized and engages the kill switch, which is exactly its job: run them against an
isolated gateway (no monitor attached) or pass --e2e-only, which drives orders the legitimate way (countersigned,
sent by the executor).

  1. account summary (funds estimate, holdings, positions, open orders)
  2. a quote for a cheap stock (IDEA by default)
  3. a resting 1-share CNC limit BUY at its lower circuit, with a Countersign tag
  4. the order is found by its tag
  5. modified up by one tick (verified on the order itself)
  6. cancelled, and confirmed Cancelled

  --fault-drill (needs CS_021_TEST_FAULTS=true on the gateway): the gateway drops its HTTP answer AFTER 021 accepted a
  placement; reconciliation must find the order, a replay of the same tag must send nothing, and exactly one order
  must exist. The drill order is then cancelled.

  --e2e EMAIL (EMAIL must be listed in CS_021_OWNER_EMAILS): the full trader path through the API: sign up or sign in,
  land on the 021 account, draft "buy 1 RELIANCE at its lower circuit", countersign with the PIN, let the executor
  send it through the gateway, find it on 021 by its tag, then draft and countersign its cancellation.
  --e2e-drill (with --e2e, needs CS_021_TEST_FAULTS=true): the gateway drops its answer after 021 accepted the
  countersigned order; the order must be reconciled, reported once, and never resent.

Order ids are printed; the 021 account id and credentials never are.
Usage: uv run --project backend python scripts/smoke_021.py [--base http://127.0.0.1:8200] [--symbol IDEA] [--fault-drill]"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # summaries carry the rupee sign


def keys(args) -> dict:
    k = {}
    f = ROOT / ".secrets" / "broker_keys.json"
    if f.exists():
        k = json.loads(f.read_text())
    return {"read": args.read_key or k.get("read"), "write": args.write_key or k.get("write"), "admin": args.admin_key or k.get("admin")}


class Gw:
    def __init__(self, base: str, k: dict):
        self.c = httpx.Client(base_url=base, timeout=60.0)
        self.k = k
        self.account = None

    def h(self, scope: str) -> dict:
        h = {"Authorization": f"Bearer {self.k[scope]}"}
        if self.account:
            h["X-Account"] = self.account
        return h

    def get(self, path: str, scope: str = "read", **kw):
        return self.c.get(path, headers=self.h(scope), **kw)

    def send(self, method: str, path: str, body: dict | None = None):
        return self.c.request(method, path, headers=self.h("write" if not path.startswith("/admin") else "admin"), json=body)


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


def place_resting(g: Gw, inst: dict, price: int, tag: str):
    body = {"instrument_id": inst["instrument_id"], "symbol": inst["symbol"], "segment": "EQ", "side": "BUY", "quantity": 1, "order_type": "LIMIT", "product": "CNC", "validity": "DAY", "limit_price_paise": price, "tag": tag}
    return g.send("POST", "/v1/orders", body)


def cancel_and_confirm(g: Gw, oid: str) -> dict:
    r = g.send("DELETE", f"/v1/orders/{oid}")
    out = r.json()
    print(f"  cancel answer: HTTP {r.status_code} status={out.get('status')} status_021={out.get('status_021')} cancellation={out.get('cancellation')} {out.get('message') or ''}".rstrip())
    end = time.time() + 15
    o = out
    while time.time() < end:
        o = g.get(f"/v1/orders/{oid}").json()
        if o.get("status_021") in ("Cancelled", "Executed", "Rejected"):
            break
        time.sleep(1.0)
    return o


def e2e(g: Gw, email: str, password: str, pin: str, api: str = "http://127.0.0.1:8000", drill: bool = False) -> None:
    c = httpx.Client(base_url=api, timeout=90, headers={"Origin": "http://localhost:3000"})
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        r = c.post("/api/auth/signup", json={"display_name": "021 smoke", "email": email, "password": password})
    check(r.status_code in (200, 201), f"signed in as the 021 owner test user (HTTP {r.status_code})")
    me = r.json()
    csrf = {"X-CSRF-Token": c.cookies.get("cs_csrf")}
    check((me.get("account") or {}).get("kind") == "021", f"landed on the 021 account ({(me.get('account') or {}).get('label')})")
    if not me.get("has_pin"):
        c.post("/api/auth/pin", json={"pin": pin}, headers=csrf)

    def chat(text: str) -> str:
        pid = None
        with c.stream("POST", "/api/chat", json={"message": text, "engine": "deterministic"}, headers=csrf) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev.get("type") == "card" and ev["card"].get("kind") == "proposal":
                        pid = ev["card"]["proposal_id"]
        check(pid is not None, f"drafted: {text}")
        p = c.get(f"/api/proposals/{pid}").json()
        print(f"  proposal {p['status']}: {p['summary']}")
        return pid

    def countersign(pid: str, targets: set[str]) -> dict:
        st = c.post(f"/api/proposals/{pid}/approval/start", json={"method": "pin"}, headers=csrf).json()
        done = c.post(f"/api/proposals/{pid}/approval/complete", json={"approval_id": st["approval_id"], "pin": pin}, headers=csrf)
        check(done.status_code == 200, f"countersigned with the PIN (HTTP {done.status_code})")
        end = time.time() + 60
        p = {}
        while time.time() < end:
            p = c.get(f"/api/proposals/{pid}").json()
            if p["status"] in targets:
                break
            time.sleep(1.0)
        return p

    q = g.get("/v1/market/quote/NSE:EQ:RELIANCE").json()
    low = int(q["lower_circuit_paise"])
    step(8, f"E2E: trader buys 1 RELIANCE at {low / 100:.2f} (resting): executor -> gateway -> 021")
    pid = chat(f"buy 1 RELIANCE at {low / 100:.2f}")
    if drill:
        r = g.c.post("/admin/faults", headers={"Authorization": f"Bearer {g.k['admin']}"}, json={"mode": "drop_after_accept", "count": 1})
        check(r.status_code == 200, "fault armed: the gateway will lose its answer AFTER 021 accepts the next order")
    t0 = time.time()
    p = countersign(pid, {"EXECUTING", "COMPLETED", "FAILED"})
    check(p["status"] in ("EXECUTING", "COMPLETED"), f"proposal {p['status']}")
    end = time.time() + 30  # EXECUTING is set before the send; wait for the broker's verdict to be recorded
    outs: list = []
    while time.time() < end:
        outs = [(o["kind"], o["title"], o["detail"]) for o in c.get("/api/outcomes").json() if o.get("proposal_id") == pid]
        if any(k in ("ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_NOT_PLACED", "ORDER_NEEDS_REVIEW", "ORDER_REJECTED") for k, _, _ in outs):
            break
        time.sleep(1.0)
    for kind, title, detail in outs[:4]:
        print(f"  outcome {kind}: {title}: {detail[:150]}")
    orders = c.get("/api/account/orders", params={"status": "all"}).json()
    tag = next((o.get("client_order_id") for o in orders if o["instrument"]["symbol"] == "RELIANCE" and o.get("price_021_paise") == low and str(o.get("client_order_id") or "").startswith("CS") and (o.get("placed_at") or "") >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 - 5))), None)
    rel = [o for o in orders if o.get("client_order_id") == tag] if tag else []
    check(len(rel) == 1, f"exactly one 021 order carries the executor's tag {tag}: {[o['broker_order_id'] for o in rel]}")
    if drill:
        check(any(k == "RECONCILED" for k, _, _ in outs), "the lost answer was reconciled (outcome RECONCILED), not resent")
        book = g.get("/admin/orders", scope="admin").json()
        same = [x for x in book if x.get("token_021") == q["token_021"] and x.get("price_021_paise") == low and (x.get("placed_at") or "") >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 - 5))]
        check(len(same) == 1, f"021 holds exactly one such order: {[x['broker_order_id'] for x in same]}")
    step(9, "E2E: trader cancels it")
    pid2 = chat("cancel my RELIANCE order")
    p2 = countersign(pid2, {"COMPLETED", "FAILED"})
    check(p2["status"] == "COMPLETED", f"cancellation {p2['status']}")
    o = g.get(f"/v1/orders/{rel[0]['broker_order_id']}").json()
    check(o.get("status_021") == "Cancelled", f"order {rel[0]['broker_order_id']} is Cancelled on 021")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8200")
    ap.add_argument("--symbol", default="IDEA")
    ap.add_argument("--fault-drill", action="store_true")
    ap.add_argument("--read-key")
    ap.add_argument("--write-key")
    ap.add_argument("--admin-key")
    ap.add_argument("--e2e", metavar="EMAIL", help="an email listed in CS_021_OWNER_EMAILS (test user)")
    ap.add_argument("--e2e-password", default="smoke-021-password-not-for-real-use")
    ap.add_argument("--e2e-pin", default="2580")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--e2e-only", action="store_true", help="skip the direct-gateway order steps (use on a full stack)")
    ap.add_argument("--e2e-drill", action="store_true")
    args = ap.parse_args()
    g = Gw(args.base, keys(args))
    try:
        h = g.c.get("/health", timeout=5).json()
    except httpx.HTTPError:
        print(f"gateway021 is not running at {args.base}; nothing to check (start the stack with 021 credentials in .env).")
        sys.exit(2)
    step(0, "Gateway")
    check(bool(h.get("logged_in")), f"logged in to 021 (account {h.get('account')}, token valid until {h.get('token_expires_at')})")
    g.account = g.get("/v1/account").json()["account_id"]

    step(1, "Account summary")
    f = g.get("/v1/funds").json()
    hold = g.get("/v1/holdings").json()
    pos = g.get("/v1/positions").json()
    opn = g.get("/v1/orders", params={"status": "open"}).json()
    print(f"  funds (estimate={f.get('estimate')}): available Rs {f['available_paise'] / 100:,.2f}; holdings {len(hold)}; positions {len(pos)}; open orders {len(opn)}")
    check(f.get("estimate") is True, "funds are labelled as an estimate (021 has no funds API)")

    step(2, f"Quote {args.symbol}")
    q = g.get(f"/v1/market/quote/NSE:EQ:{args.symbol}").json()
    t = q.get("tick") or {}
    print(f"  token {q['token_021']} tick {q['tick_size_paise']} paise, circuits {q['lower_circuit_paise']}..{q['upper_circuit_paise']}, LTP {t.get('ltp_paise')} (age {t.get('stale') and 'stale' or 'fresh'})")
    check(q.get("token_021") is not None, "instrument resolved to a 021 token")

    if args.e2e_only:
        if args.e2e:
            e2e(g, args.e2e, args.e2e_password, args.e2e_pin, api=args.api, drill=args.e2e_drill)
        print("\nsmoke_021: all checks passed")
        return

    tick = int(q["tick_size_paise"]) or 1
    price = int(q["lower_circuit_paise"])
    price -= price % tick
    inst = {"instrument_id": q["instrument_id"], "symbol": q["symbol"]}
    tag = "CSSMK" + secrets.token_hex(5).upper()

    step(3, f"Place 1 {args.symbol} CNC limit BUY at the lower circuit ({price} paise), tag {tag}")
    r = place_resting(g, inst, price, tag)
    o = r.json()
    check(r.status_code == 200, f"accepted: HTTP {r.status_code} orderId {o.get('broker_order_id')} status {o.get('status')} ({o.get('status_021')}) {o.get('message') or ''}".rstrip())
    oid = o["broker_order_id"]

    step(4, "Find it by its Countersign tag")
    rows = g.get("/v1/orders", params={"tag": tag}).json()
    check(len(rows) == 1 and rows[0]["broker_order_id"] == oid and rows[0]["client_order_id"] == tag, f"exactly one order carries tag {tag}: {oid}")

    step(5, "Modify up by one tick (verified on the order itself)")
    r = g.send("PUT", f"/v1/orders/{oid}", {"limit_price_paise": price + tick})
    m = r.json()
    print(f"  modify answer: HTTP {r.status_code} modification={m.get('modification')} price_021={m.get('price_021_paise')} {m.get('message') or ''}".rstrip())
    if r.status_code == 409:
        print("  NOTE  the sandbox filled or closed the order before the modification (its simulated exchange); not a gateway fault")
    else:
        check(r.status_code == 200 and (m.get("price_021_paise") == price + tick or m.get("modification") == "PENDING"), "modification applied on 021 (or accepted and pending)")

    step(6, "Cancel and confirm")
    final = cancel_and_confirm(g, oid)
    if final.get("status_021") == "Executed":
        print("  NOTE  the sandbox executed the order before the cancellation; the gateway reports exactly what 021 shows")
    else:
        check(final.get("status_021") == "Cancelled", f"order {oid} is Cancelled on 021")

    if args.fault_drill:
        step(7, "Fault drill: drop the gateway's answer AFTER 021 accepted the order")
        r = g.c.post("/admin/faults", headers={"Authorization": f"Bearer {g.k['admin']}"}, json={"mode": "drop_after_accept", "count": 1})
        if r.status_code != 200:
            print(f"  SKIP  fault drills are disabled on this gateway ({r.json().get('code')}); start it with CS_021_TEST_FAULTS=true")
        else:
            tag2 = "CSDRL" + secrets.token_hex(5).upper()
            t0 = time.time()
            r = place_resting(g, inst, price, tag2)
            o2 = r.json()
            print(f"  answer after {time.time() - t0:.1f}s: HTTP {r.status_code} reconciled={o2.get('reconciled')} orderId {o2.get('broker_order_id')} original error: {o2.get('original_error')}")
            check(r.status_code == 200 and o2.get("reconciled") is True, "reconciliation found the accepted order in 021's order book")
            r = place_resting(g, inst, price, tag2)
            o3 = r.json()
            check(o3.get("broker_order_id") == o2.get("broker_order_id") and o3.get("replayed_tag") is True, "a replay of the same tag sent nothing new (same order returned)")
            book = g.get("/admin/orders", scope="admin").json()
            same = [x for x in book if x.get("token_021") == q["token_021"] and x.get("price_021_paise") == price and x.get("placed_at") and x["placed_at"] >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 - 5))]
            check(len(same) == 1, f"exactly one matching order exists at 021 for the drill ({[x['broker_order_id'] for x in same]})")
            att = g.get(f"/v1/attempts/{tag2}").json()
            print(f"  gateway record: state {att.get('state')}, note: {att.get('note')}")
            final2 = cancel_and_confirm(g, o2["broker_order_id"])
            print(f"  drill order {o2['broker_order_id']} ended {final2.get('status_021')}")
    if args.e2e:
        e2e(g, args.e2e, args.e2e_password, args.e2e_pin, api=args.api, drill=args.e2e_drill)
    print("\nsmoke_021: all checks passed")


if __name__ == "__main__":
    main()
