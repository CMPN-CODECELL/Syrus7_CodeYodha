"""WhatsApp alerts: shared helpers for the API and the notifier process.

WhatsApp is a notification and read-only channel. Nothing reachable from it can approve, place, modify or cancel an
order or a standing instruction; approvals stay passkey / app PIN / authenticator code inside the app.

The Twilio credentials exist only in the notifier process. The API reaches it over loopback with the shared notifier
key (X-Notifier-Key), and this module holds no secret."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from countersign.core.config import settings
from countersign.core.market import IST, NSE_HOLIDAYS_2026
from countersign.core.models import Account, AccountMember, BrokerLink, WhatsAppLink, WhatsAppMessage, WhatsAppPrefs

SANDBOX_NUMBER = "+14155238886"
WATCH_INTERVALS = (2, 5, 10, 15, 30)
MAX_WATCHES = 10
CODE_TTL = timedelta(minutes=10)
CODE_MAX_ATTEMPTS = 5
USER_LIMIT = (20, 600.0)  # messages per user per 10 minutes
GLOBAL_LIMIT = (300, 600.0)  # messages from this installation per 10 minutes
CATEGORIES = ("orders", "mandates", "security", "digests")

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
INDIA_RE = re.compile(r"^\+91[6-9]\d{9}$")

# Message statuses the UI shows: SENT, LOGGED (log-only mode), FAILED_WINDOW (Twilio 63016, outside WhatsApp's 24-hour
# window), FAILED (any other failure; error_code and a hint say why) and PAUSED (an alert held back by PAUSE).
# Twilio error codes the UI can explain, as (reason, hint).
TWILIO_ERRORS = {
    63016: ("FAILED_WINDOW", "WhatsApp only delivers to you within 24 hours of your last message to the sandbox. Send any message to the sandbox number to reopen the 24-hour window."),
    63015: ("NOT_JOINED", "This number has not joined the Twilio sandbox. Send 'join <your code>' to the sandbox number from WhatsApp (find the code in the Twilio console)."),
    63007: ("SENDER_INVALID", "The sender is not a WhatsApp sender on this Twilio account. Check CS_TWILIO_WHATSAPP_FROM (for the sandbox: whatsapp:+14155238886)."),
    63003: ("INVALID_NUMBER", "WhatsApp could not reach this number. Check that it is a WhatsApp number in international format."),
    21211: ("INVALID_NUMBER", "Twilio rejected the phone number. Use international format, for example +91 98765 43210."),
    21614: ("INVALID_NUMBER", "This number cannot receive WhatsApp messages."),
    21408: ("NOT_PERMITTED", "The Twilio account is not permitted to send to this region."),
    20003: ("AUTH_FAILED", "Twilio rejected the account SID or auth token configured for the notifier."),
    20429: ("TWILIO_BUSY", "Twilio is rate limiting this account. Try again in a minute."),
    63018: ("TWILIO_BUSY", "Twilio is rate limiting this account. Try again in a minute."),
}
LOCAL_RATE_LIMIT = 429  # error_code of a message this installation held back (its own rate limit), never Twilio's
STATUS_HINTS = {reason: hint for reason, hint in TWILIO_ERRORS.values()}
STATUS_HINTS["RATE_LIMITED"] = "Too many WhatsApp messages in the last 10 minutes; this one was not sent."
STATUS_HINTS["LOGGED"] = "Log-only mode: Twilio is not configured, so the message was recorded but not sent."
STATUS_HINTS["NOTIFIER_DOWN"] = "The notifier process is not running, so nothing was sent."
STATUS_HINTS["PAUSED"] = "Alerts were paused, so this one was not sent."
WINDOW_HINT = STATUS_HINTS["FAILED_WINDOW"]


def status_for(error_code: int | None) -> str:
    return "FAILED_WINDOW" if error_code == 63016 else "FAILED"


def hint_for(status: str, error_code: int | None) -> str | None:
    if error_code == LOCAL_RATE_LIMIT:
        return STATUS_HINTS["RATE_LIMITED"]
    if error_code is not None and error_code in TWILIO_ERRORS:
        return TWILIO_ERRORS[error_code][1]
    if status == "FAILED" and error_code:
        return f"Twilio refused the message (error {error_code})."
    return STATUS_HINTS.get(status)


def normalize_phone(raw: str) -> str | None:
    """E.164 (+<country><number>) or None. Spaces, dots, dashes and brackets are ignored; India numbers must be
    +91 followed by a 10-digit mobile number."""
    s = re.sub(r"[\s().-]", "", raw or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    if not E164_RE.match(s):
        return None
    if s.startswith("+91") and not INDIA_RE.match(s):
        return None
    return s


def mask_phone(phone: str | None) -> str:
    """+91******3210: the country code and the last four digits only."""
    p = (phone or "").replace("whatsapp:", "")
    if len(p) < 8:
        return "***"
    cc = 3 if p.startswith(("+91", "+44", "+61", "+65", "+49", "+33", "+81", "+86")) else 2
    return p[:cc] + "*" * (len(p) - cc - 4) + p[-4:]


def display_number(phone: str) -> str:
    """The public Twilio sandbox number in the readable form the console shows; any other sender stays masked."""
    p = phone.replace("whatsapp:", "")
    if p == SANDBOX_NUMBER:
        return "+1 415 523 8886"
    return mask_phone(p)


def code_hash(code_id: str, code: str) -> str:
    return hashlib.sha256(f"wa-code:{code_id}:{code}".encode()).hexdigest()


def code_matches(code_id: str, code: str, stored: str) -> bool:
    return hmac.compare_digest(code_hash(code_id, code), stored)


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def preview(body: str, n: int = 60) -> str:
    """A short, secret-free preview for the log: verification codes are never stored."""
    text = re.sub(r"\b\d{6}\b", "******", body.replace("\n", " "))
    return text if len(text) <= n else text[: n - 3] + "..."


def prefs_of(s: Session, user_id: str) -> WhatsAppPrefs:
    p = s.get(WhatsAppPrefs, user_id)
    if p is None:
        p = WhatsAppPrefs(user_id=user_id, orders=True, mandates=True, security=True, digests=False)
        s.add(p)
        s.flush()
    return p


def prefs_view(p: WhatsAppPrefs | None) -> dict:
    if p is None:
        return {"orders": True, "mandates": True, "security": True, "digests": False}
    return {"orders": p.orders, "mandates": p.mandates, "security": p.security, "digests": p.digests}


def deliverable(link: WhatsAppLink | None, now: datetime | None = None) -> bool:
    """Verified, opted in and not paused."""
    now = now or datetime.now(UTC)
    return bool(link and link.verified_at and link.opted_in and not (link.paused_until and link.paused_until > now))


def wants(s: Session, user_id: str, category: str) -> bool:
    if category not in CATEGORIES:
        return True
    return bool(prefs_view(s.get(WhatsAppPrefs, user_id))[category])


def window_hint(link: WhatsAppLink | None) -> str | None:
    if link is None or link.window_closed_at is None:
        return None
    if link.last_inbound_at and link.last_inbound_at > link.window_closed_at:
        return None
    return WINDOW_HINT


def operators(s: Session, account_id: str) -> set[str]:
    """Users who operate an account: its owner, members and self-service links."""
    acct = s.get(Account, account_id)
    if acct is None:
        return set()
    out = {acct.user_id}
    out |= set(s.execute(select(AccountMember.user_id).where(AccountMember.account_id == account_id)).scalars())
    out |= set(s.execute(select(BrokerLink.user_id).where(BrokerLink.account_id == account_id)).scalars())
    return out


def recent_count(s: Session, since: datetime, user_id: str | None = None) -> int:
    from sqlalchemy import func

    q = select(func.count()).select_from(WhatsAppMessage).where(
        WhatsAppMessage.direction == "out",
        WhatsAppMessage.ts >= since,
        WhatsAppMessage.status != "PAUSED",
        (WhatsAppMessage.error_code.is_(None)) | (WhatsAppMessage.error_code != LOCAL_RATE_LIMIT),
    )
    if user_id is not None:
        q = q.where(WhatsAppMessage.user_id == user_id)
    return int(s.execute(q).scalar_one())


def nse_open(now: datetime | None = None) -> bool:
    """The real NSE cash session (09:15 to 15:30 IST, weekdays, not a holiday), independent of demo mode."""
    t = (now or datetime.now(UTC)).astimezone(IST)
    if t.weekday() >= 5 or t.date() in NSE_HOLIDAYS_2026:
        return False
    m = t.hour * 60 + t.minute
    return 9 * 60 + 15 <= m < 15 * 60 + 30


def pct(x: float) -> str:
    """+0.2% / −0.4% (a minus sign, as WhatsApp shows it)."""
    return ("+" if x >= 0 else "−") + f"{abs(x):.1f}%"


def public_app_url() -> str:
    """The app's public origin, derived from the webhook URL Twilio calls."""
    u = settings().whatsapp_public_url
    m = re.match(r"^(https?://[^/]+)", u)
    return m.group(1) if m else "https://countersign.everyrepo.me"


# ------------------------------------------------------------------ the API side of the notifier link (loopback)


class NotifierDown(Exception):
    pass


_client: list[httpx.Client] = []


def notifier_http() -> httpx.Client:
    """Loopback client to the notifier (tests replace it with one bound to the notifier app)."""
    if not _client:
        _client.append(httpx.Client(base_url=settings().notifier_url, timeout=10.0))
    return _client[0]


def notifier_call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    try:
        r = notifier_http().request(method, path, json=body, headers={"X-Notifier-Key": settings().notifier_key})
    except httpx.HTTPError as e:
        raise NotifierDown(str(e)) from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


def notifier_status() -> dict:
    try:
        code, data = notifier_call("GET", "/health")
    except NotifierDown:
        return {"up": False, "configured": False, "mode": "log_only", "sandbox_number": display_number(SANDBOX_NUMBER)}
    if code != 200:
        return {"up": False, "configured": False, "mode": "log_only", "sandbox_number": display_number(SANDBOX_NUMBER)}
    return {"up": True, "configured": bool(data.get("configured")), "mode": data.get("mode", "log_only"), "sandbox_number": data.get("sandbox_number") or display_number(SANDBOX_NUMBER)}


def send_via_notifier(user_id: str, kind: str, body: str, *, pending: bool = False) -> dict:
    """Asks the notifier to send one message to this user's linked number. pending=True only for the verification
    code (the number is not verified yet). Returns {status, hint, message_id}."""
    try:
        code, data = notifier_call("POST", "/v1/send", {"user_id": user_id, "kind": kind, "body": body, "pending": pending})
    except NotifierDown:
        return {"status": "FAILED", "hint": STATUS_HINTS["NOTIFIER_DOWN"], "message_id": None, "error_code": None}
    if code != 200:
        return {"status": "FAILED", "hint": data.get("hint") or data.get("message"), "message_id": data.get("message_id"), "error_code": data.get("error_code")}
    return {"status": data.get("status"), "hint": data.get("hint"), "message_id": data.get("message_id"), "error_code": data.get("error_code")}


def log_message(s: Session, *, user_id: str | None, kind: str, phone: str | None, body: str, status: str, direction: str = "out", twilio_sid: str | None = None, error_code: int | None = None, source_ref: str | None = None) -> WhatsAppMessage:
    m = WhatsAppMessage(
        user_id=user_id,
        direction=direction,
        kind=kind,
        phone_masked=mask_phone(phone),
        body_hash=body_hash(body),
        preview=preview(body),
        status=status,
        twilio_sid=twilio_sid,
        error_code=error_code,
        source_ref=source_ref,
    )
    s.add(m)
    s.flush()
    return m
