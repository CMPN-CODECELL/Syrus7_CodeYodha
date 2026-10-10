"""Notifier process (loopback HTTP on 127.0.0.1:8300): the only holder of the Twilio credentials.

  GET  /health               mode (live | log_only); never any credential
  POST /v1/send              one message to a user's linked number (verification code, welcome, test)
  POST /v1/inbound/verify    checks a Twilio webhook signature (only this process holds the auth token)

Every /v1 call needs the shared notifier key (X-Notifier-Key). A background loop turns the app's events into alerts,
sends due price watches and reads delivery statuses back. WhatsApp is a notification and read-only channel: nothing
here can approve, place, modify or cancel anything."""

from __future__ import annotations

import hmac
import sys
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from countersign.core import whatsapp as wa
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.notifier import service, twilio

app = FastAPI(title="Countersign notifier", version="1.0")
SEND_KINDS = {"verify", "welcome", "test"}


def _key(request: Request) -> None:
    expected = settings().notifier_key
    if not expected:
        raise HTTPException(503, {"code": "NO_NOTIFIER_KEY", "message": "The notifier key is not configured."})
    if not hmac.compare_digest(request.headers.get("x-notifier-key", ""), expected):
        raise HTTPException(401, {"code": "BAD_NOTIFIER_KEY", "message": "Wrong or missing notifier key."})


@app.exception_handler(HTTPException)
def _http_err(_: Request, e: HTTPException):
    return JSONResponse(e.detail if isinstance(e.detail, dict) else {"code": "ERROR", "message": str(e.detail)}, status_code=e.status_code)


@app.get("/health")
def health():
    c = service.cfg()
    return {
        "ok": True,
        "mode": service.mode(),
        "configured": c.configured,
        "sandbox_number": wa.display_number(c.sender),
        "sender_valid": c.sender_valid,  # false: CS_TWILIO_WHATSAPP_FROM is missing or not a phone number (sandbox used)
    }


class SendBody(BaseModel):
    user_id: str
    kind: str
    body: str
    pending: bool = False


@app.post("/v1/send")
def send(b: SendBody, request: Request):
    _key(request)
    if b.kind not in SEND_KINDS:
        raise HTTPException(422, {"code": "BAD_KIND", "message": "Unknown message kind."})
    if not b.body.strip() or len(b.body) > 1500:
        raise HTTPException(422, {"code": "BAD_BODY", "message": "Message body must be 1 to 1500 characters."})
    with session_scope() as s:
        return service.deliver(s, b.user_id, b.kind, b.body, pending=b.pending and b.kind == "verify")


class InboundBody(BaseModel):
    url: str
    body: str
    signature: str | None = None


@app.post("/v1/inbound/verify")
def inbound_verify(b: InboundBody, request: Request):
    _key(request)
    c = service.cfg()
    if not c.configured:
        raise HTTPException(503, {"code": "NOT_CONFIGURED", "message": "Twilio is not configured (log-only mode), so webhook signatures cannot be checked."})
    if not twilio.valid_signature(c, b.url, b.body, b.signature):
        raise HTTPException(403, {"code": "BAD_SIGNATURE", "valid": False, "message": "Twilio signature missing or invalid."})
    return {"valid": True}


def _loop(stop: threading.Event) -> None:
    n = 0
    while not stop.is_set():
        for every, fn in ((1, service.pump_once), (7, service.watches_once), (5, service.refresh_statuses)):
            if n % every:
                continue
            try:
                with session_scope() as s:
                    fn(s)
            except Exception as e:  # noqa: BLE001
                print(f"[notifier] {fn.__name__} failed: {type(e).__name__}", flush=True)
        n += 1
        stop.wait(2.0)


_stop = threading.Event()


def main() -> None:
    import uvicorn

    cfg = settings()
    if cfg.broker_write_key or cfg.broker_read_key or cfg.broker_admin_key:
        sys.exit("notifier: refusing to start. The notifier holds no broker key (credential zone violation).")
    if not cfg.notifier_key:
        sys.exit("notifier: refusing to start without CS_NOTIFIER_KEY (scripts/dev.py generates it).")
    # The schema is created by scripts/dev.py (bootstrap) before any process starts.
    print(f"notifier: mode {service.mode()} on 127.0.0.1:{service.cfg().port}", flush=True)
    if service.cfg().configured and not service.cfg().sender_valid:
        print("notifier: CS_TWILIO_WHATSAPP_FROM is missing or not a phone number; sending from the Twilio sandbox number", flush=True)
    threading.Thread(target=_loop, args=(_stop,), name="notifier-loop", daemon=True).start()
    time.sleep(0.1)
    uvicorn.run(app, host="127.0.0.1", port=service.cfg().port, log_level="warning")


if __name__ == "__main__":
    main()
