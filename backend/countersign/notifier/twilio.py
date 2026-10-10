"""Twilio REST client and webhook signature check (notifier process only).

Each message is sent at most once: a failed send is recorded with its status and never retried in a loop."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qsl

import httpx

from countersign.core.broker_client import _tls
from countersign.core.whatsapp import status_for
from countersign.notifier.settings import TwilioSettings

# Tests replace this with an httpx.MockTransport; production uses the real network with the shared TLS context.
TRANSPORT: list[httpx.BaseTransport] = []


def _client(cfg: TwilioSettings) -> httpx.Client:
    kw: dict = {"timeout": cfg.timeout_s, "auth": (cfg.account_sid, cfg.auth_token.get_secret_value())}
    if TRANSPORT:
        kw["transport"] = TRANSPORT[0]
    else:
        kw["verify"] = _tls()
    return httpx.Client(base_url=cfg.api_base, **kw)


def classify(error_code: int | None) -> str:
    """FAILED_WINDOW for 63016 (outside the 24-hour window), FAILED for anything else (the error code says why)."""
    return status_for(int(error_code) if error_code is not None else None)


def send(cfg: TwilioSettings, to_e164: str, body: str) -> dict:
    """POST .../Messages.json once. Returns {status, sid, error_code}. Status SENT means Twilio accepted it."""
    data = {"From": cfg.sender, "To": "whatsapp:" + to_e164, "Body": body}
    try:
        with _client(cfg) as c:
            r = c.post(f"/2010-04-01/Accounts/{cfg.account_sid}/Messages.json", data=data)
    except httpx.HTTPError:
        return {"status": "FAILED", "sid": None, "error_code": None}
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code in (200, 201):
        code = j.get("error_code")
        if code:
            return {"status": classify(code), "sid": j.get("sid"), "error_code": int(code)}
        return {"status": "SENT", "sid": j.get("sid"), "error_code": None}
    code = j.get("code")
    return {"status": classify(code), "sid": None, "error_code": int(code) if code else None}


def fetch_status(cfg: TwilioSettings, sid: str) -> dict:
    """Reads a message's delivery status back (63016 and 63015 usually arrive here, after the send was accepted)."""
    try:
        with _client(cfg) as c:
            r = c.get(f"/2010-04-01/Accounts/{cfg.account_sid}/Messages/{sid}.json")
        j = r.json()
    except (httpx.HTTPError, ValueError):
        return {"status": None, "error_code": None}
    if r.status_code != 200:
        return {"status": None, "error_code": None}
    code = j.get("error_code")
    tw = str(j.get("status") or "")
    if code or tw in ("failed", "undelivered"):
        return {"status": classify(code) if code else "FAILED", "error_code": int(code) if code else None}
    return {"status": "SENT", "error_code": None, "final": tw in ("delivered", "read")}


def signature(auth_token: str, url: str, params: list[tuple[str, str]]) -> str:
    """Twilio's algorithm: HMAC-SHA1 over the full URL followed by every POST parameter (sorted by name) as name+value,
    base64 encoded."""
    payload = url + "".join(k + v for k, v in sorted(params, key=lambda kv: kv[0]))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()).decode()


def valid_signature(cfg: TwilioSettings, url: str, raw_body: str, presented: str | None) -> bool:
    token = cfg.auth_token.get_secret_value()
    if not token or not presented:
        return False
    params = parse_qsl(raw_body, keep_blank_values=True)
    return hmac.compare_digest(signature(token, url, params), presented)
