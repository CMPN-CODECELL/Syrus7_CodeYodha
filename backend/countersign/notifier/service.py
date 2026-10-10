"""What the notifier does: deliver one message (checks, rate limits, log-only mode, one Twilio send), turn the app's
durable events into alerts, run price watches and read delivery statuses back from Twilio.

Events are read from Postgres, never only from the bus: the outcomes table (orders, standing instructions, kill
switch, out-of-band orders) and the append-only audit chain (expired and cancelled instructions, sign-ins, security
events). Each alert is sent at most once per user: whatsapp_messages has a unique (user_id, source_ref)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from countersign.core import market, state
from countersign.core import whatsapp as wa
from countersign.core.market import IST
from countersign.core.models import AuditEvent, OutcomeRow, SecurityEvent, SessionRow, StandingMandate, User, WhatsAppLink, WhatsAppMessage, WhatsAppWatch
from countersign.core.money import inr
from countersign.core.schemas import ALL_HOLDINGS_ID
from countersign.notifier import twilio
from countersign.notifier.settings import TwilioSettings, twilio_settings

log = logging.getLogger("countersign.notifier")

# Tests swap the settings for a fixed TwilioSettings (live or log-only) without touching .env.
CONFIG: list[TwilioSettings] = []


def cfg() -> TwilioSettings:
    return CONFIG[0] if CONFIG else twilio_settings()


def mode() -> str:
    return "live" if cfg().configured else "log_only"


# ------------------------------------------------------------------ one message


def deliver(s: Session, user_id: str, kind: str, body: str, *, category: str | None = None, source_ref: str | None = None, pending: bool = False, event_at: datetime | None = None) -> dict:
    """Sends one message to a user's linked number, at most once. category set: an alert, which also needs a
    verified, opted-in, unpaused link and the matching preference. pending: the verification code (unverified link)."""
    now = datetime.now(UTC)
    link = s.get(WhatsAppLink, user_id)
    if link is None:
        return {"status": "NOT_LINKED", "hint": "Link a WhatsApp number first.", "message_id": None}
    if not pending:
        if link.verified_at is None:
            return {"status": "NOT_VERIFIED", "hint": "Verify your WhatsApp number first.", "message_id": None}
        if not link.opted_in:
            return {"status": "OPTED_OUT", "hint": "You sent STOP. Send START to the sandbox number, or turn alerts back on in the app.", "message_id": None}
        if category is not None and not wa.wants(s, user_id, category):
            return {"status": "SKIPPED", "hint": None, "message_id": None}
        if category is not None and event_at is not None and link.verified_at is not None and event_at < link.verified_at - timedelta(seconds=10):
            # Alerts cover what happens after the number was verified, not events from before linking (10 s of slack
            # for the database and process clocks).
            return {"status": "SKIPPED", "hint": None, "message_id": None}
    if source_ref and s.execute(select(WhatsAppMessage.id).where(WhatsAppMessage.user_id == user_id, WhatsAppMessage.source_ref == source_ref)).first():
        return {"status": "DUPLICATE", "hint": None, "message_id": None}
    if category is not None and link.paused_until and link.paused_until > now:
        # Recorded (once per event) so the trader sees what PAUSE held back; never sent later.
        m = wa.log_message(s, user_id=user_id, kind=kind, phone=link.phone_e164, body=body, status="PAUSED", source_ref=source_ref)
        return {"status": "PAUSED", "hint": wa.STATUS_HINTS["PAUSED"], "message_id": m.id, "error_code": None}
    window = now - timedelta(seconds=wa.USER_LIMIT[1])
    if wa.recent_count(s, window, user_id) >= wa.USER_LIMIT[0] or wa.recent_count(s, now - timedelta(seconds=wa.GLOBAL_LIMIT[1])) >= wa.GLOBAL_LIMIT[0]:
        m = wa.log_message(s, user_id=user_id, kind=kind, phone=link.phone_e164, body=body, status="FAILED", error_code=wa.LOCAL_RATE_LIMIT, source_ref=source_ref)
        return {"status": "FAILED", "hint": wa.STATUS_HINTS["RATE_LIMITED"], "message_id": m.id, "error_code": wa.LOCAL_RATE_LIMIT}
    c = cfg()
    if not c.configured:
        m = wa.log_message(s, user_id=user_id, kind=kind, phone=link.phone_e164, body=body, status="LOGGED", source_ref=source_ref)
        # Log-only mode is for local development: the body (a verification code included) goes to the notifier log,
        # the phone number only masked.
        print(f"[log-only] to {wa.mask_phone(link.phone_e164)} ({kind}): {body}", flush=True)
        return {"status": "LOGGED", "hint": wa.STATUS_HINTS["LOGGED"], "message_id": m.id, "error_code": None}
    r = twilio.send(c, link.phone_e164, body)
    m = wa.log_message(s, user_id=user_id, kind=kind, phone=link.phone_e164, body=body, status=r["status"], twilio_sid=r["sid"], error_code=r["error_code"], source_ref=source_ref)
    if r["status"] == "FAILED_WINDOW":
        link.window_closed_at = now
    print(f"[send] to {wa.mask_phone(link.phone_e164)} ({kind}): {r['status']}{' error ' + str(r['error_code']) if r['error_code'] else ''}", flush=True)
    return {"status": r["status"], "hint": wa.hint_for(r["status"], r["error_code"]), "message_id": m.id, "error_code": r["error_code"]}


# ------------------------------------------------------------------ alerts from the app's events

MANDATE_KINDS = {"MANDATE_ARMED", "MANDATE_TRIGGERED", "MANDATE_NEEDS_REVIEW", "ALERT"}
ORDER_KINDS = {"ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_PARTIALLY_FILLED", "ORDER_REJECTED", "ORDER_NOT_PLACED", "ORDER_NEEDS_REVIEW", "ORDER_CANCELLED", "EXECUTION_BLOCKED", "RECONCILED"}
SECURITY_OUTCOMES = {"KILL_SWITCH", "EXTERNAL_ORDER"}
SECURITY_KINDS = {
    "PASSKEY_REGISTERED",
    "PIN_CHANGED",
    "PIN_RESET",
    "AUTHENTICATOR_LINKED",
    "AUTHENTICATOR_REPLACED",
    "BROKER_021_LINKED",
    "BROKER_021_UNLINKED",
    "BROKER_021_LINK_WRONG_CREDENTIALS",
    "UNAUTHORIZED_BROKER_ORDER",
}
AUDIT_TYPES = ("MANDATE_EXPIRED", "MANDATE_CANCELLED", "SESSION_CREATED", "SECURITY_EVENT")
CURSOR_KEY = "whatsapp_cursor"
OVERLAP = timedelta(seconds=30)  # rows committed late (Postgres now() is the transaction start) are still seen
AUDIT_OVERLAP = 200


def _short(text: str, n: int = 320) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def _tidy(text: str, n: int = 700) -> str:
    """Like _short, but keeps the line breaks that structure a WhatsApp message."""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in (text or "").strip().split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text if len(text) <= n else text[: n - 3] + "..."


ORDER_HEADS = {"Filled": "Order filled", "Rejected": "Order rejected", "Partially filled": "Order partly filled", "Cancelled": "Order cancelled", "Not placed": "Order not placed"}


def _titled(title: str, detail: str = "", *, footer: str = "") -> str:
    """'Order filled: BUY 10 RELIANCE' + detail -> a bold title line, the subject below it, then the detail."""
    head, _, subject = title.partition(": ")
    head = ORDER_HEADS.get(head.strip(), head.strip())
    parts = [f"*{head}*" + (f"\n{subject.strip()}" if subject.strip() else "")]
    if detail and not detail.startswith(title):
        parts.append(detail.strip())
    if footer:
        parts.append(f"_{footer}_")
    return _tidy("\n\n".join(parts))


def _mask_accounts(text: str, *account_ids: str | None) -> str:
    from countersign.broker021.linking import mask

    for a in account_ids:
        if a and len(a) > 5 and a not in ("*",) and a in text:
            text = text.replace(a, mask(a))
    return text


def _sym(instrument_id: str) -> str:
    return instrument_id.split(":")[-1]


def mandate_summary(m: StandingMandate) -> tuple[str, str]:
    """('If RELIANCE falls to ₹1,150.00 or below, buy 10 (limit ₹1,150.00).', 'Fires once · expires 17 Oct')"""
    p = m.predicate or {}
    who = "any holding" if m.instrument_id == ALL_HOLDINGS_ID else _sym(m.instrument_id)
    th = int(p.get("threshold") or 0)
    if p.get("field") == "LTP":
        cond = f"if {who} falls to {inr(th)} or below" if p.get("op") == "LTE" else f"if {who} rises to {inr(th)} or above"
    elif p.get("field") == "DAY_CHANGE_BPS":
        cond = f"if {who} is down {abs(th) / 100:.1f}% or more today"
    else:
        cond = f"if {who} falls {th / 100:.1f}% or more below your average price"
    if m.kind == "ORDER" and m.template:
        t = m.template
        act = f", {str(t.get('side', '')).lower()} {t.get('quantity')} (limit {inr(t.get('limit_price_paise'))})"
    else:
        act = ", alert you (no order is placed)"
    once = "At most once per holding per day" if m.instrument_id == ALL_HOLDINGS_ID else "Fires once"
    exp = m.expires_at.astimezone(IST)
    return f"{cond[0].upper()}{cond[1:]}{act}.", f"{once} · expires {exp.day} {exp.strftime('%b')}"


def armed_text(m: StandingMandate) -> str:
    sentence, meta = mandate_summary(m)
    return f"*Standing instruction armed*\n\n{sentence}\n{meta}"


def outcome_text(r: OutcomeRow, m: StandingMandate | None) -> str:
    if r.kind == "MANDATE_ARMED" and m is not None:
        return armed_text(m)
    if r.kind == "MANDATE_TRIGGERED":
        return _titled(r.title, r.detail)
    if r.kind == "ALERT":
        return _titled("Price alert: " + r.title.removeprefix("Alert: "), footer="No order was placed.")
    if r.kind == "KILL_SWITCH":
        if r.title.endswith("engaged"):
            return _tidy(f"*Kill switch engaged*\nCountersign will not send any order until it is released.\n\nReason: {r.detail}")
        return _tidy(f"*Kill switch released*\n{r.detail}")
    if r.kind == "EXTERNAL_ORDER":
        return _titled(r.title, r.detail)
    footer = "From your standing instruction." if m is not None and r.kind in ORDER_KINDS else ""
    return _titled(r.title, r.detail, footer=footer)


def _all_linked(s: Session) -> set[str]:
    return set(s.execute(select(WhatsAppLink.user_id).where(WhatsAppLink.verified_at.is_not(None))).scalars())


def handle_outcome(s: Session, r: OutcomeRow) -> int:
    if r.kind in MANDATE_KINDS:
        category = "mandates"
    elif r.kind in ORDER_KINDS:
        category = "orders"
    elif r.kind in SECURITY_OUTCOMES:
        category = "security"
    else:
        return 0
    if r.account_id == "*":
        if r.kind != "KILL_SWITCH":
            return 0
        users = _all_linked(s)
    else:
        users = wa.operators(s, r.account_id)
    if not users:
        return 0
    m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == r.proposal_id)).scalar_one_or_none() if r.proposal_id else None
    if m is not None and category == "orders":
        category = "mandates"
    body = _mask_accounts(outcome_text(r, m), r.account_id)
    sent = 0
    for uid in sorted(users):
        res = deliver(s, uid, r.kind.lower()[:24], body, category=category, source_ref=f"outcome:{r.id}", event_at=r.ts)
        sent += res["status"] in ("SENT", "LOGGED", "FAILED", "FAILED_WINDOW")
    return sent


def _ua_short(ua: str) -> str:
    ua = ua or ""
    browser = next((b for b, k in (("Edge", "Edg/"), ("Chrome", "Chrome/"), ("Firefox", "Firefox/"), ("Safari", "Safari/")) if k in ua), None)
    os_ = next((o for o, k in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"), ("macOS", "Mac OS X"), ("Linux", "Linux")) if k in ua), None)
    if browser and os_:
        return f"{browser} on {os_}"
    if browser or os_:
        return browser or os_ or ""
    return _short(ua, 40) or "unknown device"


def handle_audit(s: Session, a: AuditEvent) -> int:
    users: set[str] = set()
    category = "security"
    body = ""
    if a.event_type in ("MANDATE_EXPIRED", "MANDATE_CANCELLED"):
        m = s.get(StandingMandate, a.subject_id)
        if m is None:
            return 0
        category = "mandates"
        who = "Your holdings alert" if m.instrument_id == ALL_HOLDINGS_ID else f"Your instruction on {_sym(m.instrument_id)}"
        if a.event_type == "MANDATE_EXPIRED":
            body = f"*Standing instruction expired*\n{who} ended without triggering."
        else:
            body = f"*Standing instruction cancelled*\n{who} can no longer fire."
        users = {m.user_id}
    elif a.event_type == "SESSION_CREATED":
        row = s.get(SessionRow, a.subject_id)
        if row is None:
            return 0
        u = s.get(User, row.user_id)
        if u is None or u.is_lab_user:
            return 0
        method = str((a.data or {}).get("method") or "")
        how = f" · {method}" if method else ""
        body = f"*New sign-in to Countersign*\n{_ua_short(row.user_agent)}{how}\n\nNot you? Open Countersign > Security and sign that session out."
        users = {row.user_id}
    elif a.event_type == "SECURITY_EVENT":
        ev = s.get(SecurityEvent, a.subject_id)
        if ev is None or ev.kind not in SECURITY_KINDS:
            return 0
        uid = (ev.data or {}).get("user_id")
        if uid:
            users = {uid}
        elif ev.account_id and ev.account_id not in ("", "*", "LAB1"):
            users = wa.operators(s, ev.account_id)
        body = _mask_accounts(_tidy(f"*Security alert*\n{ev.message}"), ev.account_id)
    else:
        return 0
    sent = 0
    for uid in sorted(users):
        res = deliver(s, uid, a.event_type.lower()[:24], body, category=category, source_ref=f"audit:{a.id}", event_at=a.ts)
        sent += res["status"] in ("SENT", "LOGGED", "FAILED", "FAILED_WINDOW")
    return sent


def pump_once(s: Session) -> int:
    """One pass over new outcomes and audit events. The first pass only places the cursor (no replay of history)."""
    cur = state.get_state(s, CURSOR_KEY)
    if not cur:
        top = int(s.execute(select(func.max(AuditEvent.id))).scalar_one() or 0)
        # Database time: outcomes are stamped by Postgres, whose clock (in a container) may differ from this host's.
        now = s.execute(text("SELECT clock_timestamp()")).scalar_one().isoformat()
        state.put_state(s, CURSOR_KEY, {"outcome_ts": now, "audit_id": top, "floor_ts": now, "floor_id": top})
        return 0
    # The overlap never reaches back before the cursor was first placed, so nothing older is ever sent.
    floor_ts = datetime.fromisoformat(cur.get("floor_ts") or cur["outcome_ts"])
    floor_id = int(cur.get("floor_id", cur["audit_id"]))
    since = max(datetime.fromisoformat(cur["outcome_ts"]) - OVERLAP, floor_ts)
    sent = 0
    newest = datetime.fromisoformat(cur["outcome_ts"])
    for r in s.execute(select(OutcomeRow).where(OutcomeRow.ts > since).order_by(OutcomeRow.ts).limit(500)).scalars():
        sent += handle_outcome(s, r)
        newest = max(newest, r.ts)
    top = int(cur["audit_id"])
    for a in s.execute(select(AuditEvent).where(AuditEvent.id > max(top - AUDIT_OVERLAP, floor_id), AuditEvent.event_type.in_(AUDIT_TYPES)).order_by(AuditEvent.id).limit(500)).scalars():
        sent += handle_audit(s, a)
    max_id = s.execute(select(func.max(AuditEvent.id))).scalar_one() or top
    state.put_state(s, CURSOR_KEY, {"outcome_ts": newest.isoformat(), "audit_id": max(int(max_id), top), "floor_ts": floor_ts.isoformat(), "floor_id": floor_id})
    return sent


# ------------------------------------------------------------------ price watches


def watch_line(w: WhatsAppWatch, tick: market.Tick) -> str:
    day = (tick.ltp_paise - tick.prev_close_paise) * 100 / tick.prev_close_paise if tick.prev_close_paise else 0.0
    parts = [f"{wa.pct(day)} today"]
    if w.last_price_paise:
        parts.append(f"{wa.pct((tick.ltp_paise - w.last_price_paise) * 100 / w.last_price_paise)} since last update")
    return f"*{w.symbol}* {inr(tick.ltp_paise)}\n{' · '.join(parts)}"


def watches_once(s: Session, now: datetime | None = None) -> int:
    """Sends every due watch, one message per user. A watch is due interval_min after its last message; with
    min_move_pct it is sent only when the price moved at least that much since the last sent price; with
    market_hours_only it waits for the NSE session."""
    now = now or datetime.now(UTC)
    rows = s.execute(select(WhatsAppWatch, WhatsAppLink).join(WhatsAppLink, WhatsAppLink.user_id == WhatsAppWatch.user_id).where(WhatsAppWatch.active.is_(True)).order_by(WhatsAppWatch.created_at)).all()
    due: dict[str, list[tuple[WhatsAppWatch, market.Tick]]] = {}
    for w, link in rows:
        if not wa.deliverable(link, now):
            continue
        if w.last_sent_at is not None and now - w.last_sent_at < timedelta(minutes=w.interval_min) - timedelta(seconds=5):
            continue
        if w.market_hours_only and not wa.nse_open(now):
            continue
        tick = market.latest(w.instrument_id)
        if tick is None:
            continue
        if w.min_move_pct and w.last_price_paise:
            moved = abs(tick.ltp_paise - w.last_price_paise) * 100 / w.last_price_paise
            if moved < w.min_move_pct:
                continue
        due.setdefault(w.user_id, []).append((w, tick))
    sent = 0
    for uid, items in due.items():
        body = "*Price update*\n\n" + "\n\n".join(watch_line(w, t) for w, t in items)
        res = deliver(s, uid, "watch", body, category="watch")
        for w, t in items:
            # Updated whatever the outcome, so a failed send is never retried in a loop.
            w.last_sent_at = now
            w.last_price_paise = t.ltp_paise
        sent += res["status"] in ("SENT", "LOGGED")
    return sent


# ------------------------------------------------------------------ delivery status read-back


def refresh_statuses(s: Session, now: datetime | None = None) -> int:
    c = cfg()
    if not c.configured:
        return 0
    now = now or datetime.now(UTC)
    rows = s.execute(
        select(WhatsAppMessage)
        .where(
            WhatsAppMessage.status == "SENT",
            WhatsAppMessage.twilio_sid.is_not(None),
            WhatsAppMessage.ts > now - timedelta(minutes=10),
            (WhatsAppMessage.checked_at.is_(None)) | (WhatsAppMessage.checked_at < now - timedelta(seconds=20)),
        )
        .limit(20)
    ).scalars().all()
    changed = 0
    for m in rows:
        r = twilio.fetch_status(c, m.twilio_sid)
        # Delivered or read is final: moving checked_at past the 10-minute window stops further reads.
        m.checked_at = now + timedelta(hours=1) if r.get("final") else now
        if r["status"] and r["status"] != m.status:
            m.status = r["status"]
            m.error_code = r["error_code"]
            changed += 1
            if r["status"] == "FAILED_WINDOW" and m.user_id:
                link = s.get(WhatsAppLink, m.user_id)
                if link is not None:
                    link.window_closed_at = now
    return changed
