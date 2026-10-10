"""WhatsApp alerts API (/api/whatsapp). WhatsApp is a notification and read-only channel: nothing here, and no
WhatsApp command, can approve, place, modify or cancel an order or a standing instruction.

The signed-in routes use the session cookie with CSRF and origin checks like every other route. The Twilio webhook
(POST /api/whatsapp/inbound) has no session: the notifier checks Twilio's signature (it alone holds the auth token),
then this process answers the read-only command with TwiML (it alone holds the broker READ key)."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from countersign.auth.sessions import Principal, current
from countersign.core import audit, instruments, market, portfolio
from countersign.core import whatsapp as wa
from countersign.core.broker_client import BrokerError, BrokerTimeout, BrokerUnavailable, read_gateway
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import StandingMandate, User, WhatsAppCode, WhatsAppLink, WhatsAppMessage, WhatsAppWatch
from countersign.core.money import inr
from countersign.core.schemas import iso

router = APIRouter(prefix="/api/whatsapp")

JOIN_HINT = "Send 'join <your code>' to the sandbox number from WhatsApp (find the code in the Twilio console)"
CODES_PER_WINDOW = (3, timedelta(minutes=10))


def _err(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


# ------------------------------------------------------------------ views


def _link_view(link: WhatsAppLink | None) -> dict | None:
    if link is None:
        return None
    return {
        "phone_masked": wa.mask_phone(link.phone_e164),
        "verified": link.verified_at is not None,
        "opted_in": link.opted_in,
        "paused_until": iso(link.paused_until) if link.paused_until and link.paused_until > datetime.now(UTC) else None,
        "window_hint": wa.window_hint(link),
    }


def _watch_view(w: WhatsAppWatch) -> dict:
    return {
        "id": w.id,
        "symbol": w.symbol,
        "interval_min": w.interval_min,
        "min_move_pct": w.min_move_pct,
        "market_hours_only": w.market_hours_only,
        "active": w.active,
        "last_sent_at": iso(w.last_sent_at),
        "last_price": (w.last_price_paise / 100) if w.last_price_paise is not None else None,
    }


def _join_hint() -> str:
    code = re.sub(r"[^A-Za-z0-9 -]", "", settings().whatsapp_join_code).strip().removeprefix("join ").strip()
    return f"Send 'join {code}' to the sandbox number from WhatsApp" if code else JOIN_HINT


def _state(s: Session, user_id: str) -> dict:
    st = wa.notifier_status()
    link = s.get(WhatsAppLink, user_id)
    watches = s.execute(select(WhatsAppWatch).where(WhatsAppWatch.user_id == user_id).order_by(WhatsAppWatch.created_at)).scalars().all()
    # Messages Countersign sent (or held back), newest first; the trader's own inbound commands are not listed.
    recent = s.execute(select(WhatsAppMessage).where(WhatsAppMessage.user_id == user_id, WhatsAppMessage.direction == "out").order_by(WhatsAppMessage.ts.desc()).limit(10)).scalars().all()
    return {
        "configured": st["configured"],
        "mode": st["mode"],
        "notifier_up": st["up"],
        "sandbox": {"number": st["sandbox_number"], "join_hint": _join_hint()},
        "link": _link_view(link),
        "prefs": wa.prefs_view(s.get(wa.WhatsAppPrefs, user_id)),
        "watches": [_watch_view(w) for w in watches],
        "recent": [{"at": iso(m.ts), "kind": m.kind, "status": m.status, "preview": m.preview, "error_code": m.error_code, "hint": wa.hint_for(m.status, m.error_code)} for m in recent],
    }


@router.get("")
def get_state(p: Principal = Depends(current)):
    with session_scope() as s:
        return _state(s, p.user_id)


# ------------------------------------------------------------------ link and verify


class LinkBody(BaseModel):
    phone: str


@router.post("/link")
def link(body: LinkBody, p: Principal = Depends(current)):
    phone = wa.normalize_phone(body.phone)
    if phone is None:
        raise _err(422, "INVALID_PHONE", "Enter your WhatsApp number in international format, for example +91 98765 43210.")
    now = datetime.now(UTC)
    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(WhatsAppCode).where(WhatsAppCode.user_id == p.user_id, WhatsAppCode.created_at > now - CODES_PER_WINDOW[1])).scalar_one()
        if n >= CODES_PER_WINDOW[0]:
            raise _err(429, "RATE_LIMITED", "Too many codes requested. Wait 10 minutes and try again.", retry_after_s=600)
        other = s.execute(select(WhatsAppLink).where(WhatsAppLink.phone_e164 == phone, WhatsAppLink.user_id != p.user_id, WhatsAppLink.verified_at.is_not(None))).scalar_one_or_none()
        if other is not None:
            raise _err(409, "PHONE_IN_USE", "This number is already linked to another Countersign account.")
        lk = s.get(WhatsAppLink, p.user_id)
        if lk is None:
            lk = WhatsAppLink(user_id=p.user_id, phone_e164=phone, opted_in=True)
            s.add(lk)
        elif lk.phone_e164 != phone:
            lk.phone_e164, lk.verified_at, lk.window_closed_at, lk.last_inbound_at = phone, None, None, None
        s.execute(delete(WhatsAppCode).where(WhatsAppCode.user_id == p.user_id, WhatsAppCode.used_at.is_(None)))
        code = f"{secrets.randbelow(10**6):06d}"
        row = WhatsAppCode(user_id=p.user_id, phone_e164=phone, code_hash="", expires_at=now + wa.CODE_TTL)
        s.add(row)
        s.flush()
        row.code_hash = wa.code_hash(row.id, code)
        wa.prefs_of(s, p.user_id)
        audit.record(s, "trader", "WHATSAPP_CODE_SENT", "user", p.user_id, {"phone": wa.mask_phone(phone)})
        expires = row.expires_at
    delivery = wa.send_via_notifier(p.user_id, "verify", f"*Countersign verification code*\n\nYour code is *{code}*.\nIt expires in 10 minutes. Never share it with anyone.", pending=True)
    return {"phone_masked": wa.mask_phone(phone), "expires_at": iso(expires), "delivery": delivery}


class CodeBody(BaseModel):
    code: str


WELCOME = (
    "*Countersign alerts are on*\n\n"
    "You will get alerts for your orders, standing instructions and account security.\n\n"
    "*WhatsApp is read only.* It can never approve, place or cancel a trade. Approvals happen only in the app, "
    "with your passkey or PIN.\n\n"
    "Try *PNL*, *HOLDINGS* or *PRICE ITC*. Send *HELP* for every command."
)


@router.post("/verify")
def verify(body: CodeBody, p: Principal = Depends(current)):
    code = re.sub(r"\s", "", body.code or "")
    now = datetime.now(UTC)
    error: HTTPException | None = None
    # Failures are decided inside the transaction and raised after it commits, so a wrong attempt is always counted.
    with session_scope() as s:
        lk = s.get(WhatsAppLink, p.user_id)
        row = s.execute(select(WhatsAppCode).where(WhatsAppCode.user_id == p.user_id, WhatsAppCode.used_at.is_(None)).order_by(WhatsAppCode.created_at.desc()).limit(1)).scalar_one_or_none()
        if lk is None or row is None or row.phone_e164 != lk.phone_e164:
            error = _err(404, "NO_CODE", "Request a new code first.")
        elif row.expires_at <= now:
            row.used_at = now
            error = _err(410, "CODE_EXPIRED", "This code expired. Request a new one.")
        elif row.attempts >= wa.CODE_MAX_ATTEMPTS:
            row.used_at = now
            error = _err(429, "CODE_LOCKED", "Too many wrong codes. Request a new one.")
        elif not (code.isdigit() and len(code) == 6 and wa.code_matches(row.id, code, row.code_hash)):
            row.attempts += 1
            left = wa.CODE_MAX_ATTEMPTS - row.attempts
            if left <= 0:
                row.used_at = now
            error = _err(401, "CODE_INVALID", "That code is not right." + (f" {left} attempts left." if left > 0 else " Request a new code."), attempts_left=max(left, 0))
        else:
            row.used_at = now
            taken = s.execute(select(WhatsAppLink.user_id).where(WhatsAppLink.phone_e164 == lk.phone_e164, WhatsAppLink.user_id != p.user_id, WhatsAppLink.verified_at.is_not(None))).first()
            if taken:
                error = _err(409, "PHONE_IN_USE", "This number is already linked to another Countersign account.")
            else:
                lk.verified_at, lk.opted_in, lk.paused_until = now, True, None
                wa.prefs_of(s, p.user_id)
                audit.record(s, "trader", "WHATSAPP_LINKED", "user", p.user_id, {"phone": wa.mask_phone(lk.phone_e164)})
    if error is not None:
        raise error
    welcome = wa.send_via_notifier(p.user_id, "welcome", WELCOME)
    return {"verified": True, "welcome": welcome}


@router.delete("/link")
def unlink(p: Principal = Depends(current)):
    with session_scope() as s:
        lk = s.get(WhatsAppLink, p.user_id)
        s.execute(delete(WhatsAppCode).where(WhatsAppCode.user_id == p.user_id))
        if lk is not None:
            audit.record(s, "trader", "WHATSAPP_UNLINKED", "user", p.user_id, {"phone": wa.mask_phone(lk.phone_e164)})
            s.delete(lk)
    return {"unlinked": True}


class PrefsBody(BaseModel):
    orders: bool
    mandates: bool
    security: bool
    digests: bool


@router.put("/prefs")
def put_prefs(body: PrefsBody, p: Principal = Depends(current)):
    with session_scope() as s:
        pr = wa.prefs_of(s, p.user_id)
        pr.orders, pr.mandates, pr.security, pr.digests = body.orders, body.mandates, body.security, body.digests
        return wa.prefs_view(pr)


# ------------------------------------------------------------------ watches


class WatchBody(BaseModel):
    symbol: str
    interval_min: int
    min_move_pct: float | None = None
    market_hours_only: bool = False


def add_watch(s: Session, user_id: str, symbol: str, interval_min: int, min_move_pct: float | None, market_hours_only: bool) -> WhatsAppWatch:
    """Creates or updates a watch. Raises HTTPException with a message the trader can read."""
    if interval_min not in wa.WATCH_INTERVALS:
        raise _err(422, "BAD_INTERVAL", "Interval must be 2, 5, 10, 15 or 30 minutes.")
    if min_move_pct is not None and not (0 < min_move_pct <= 50):
        raise _err(422, "BAD_MIN_MOVE", "Minimum move must be between 0 and 50 percent.")
    r = instruments.resolve(s, (symbol or "").strip()[:40])
    if r.status != "RESOLVED" or r.instrument is None:
        raise _err(422, "UNKNOWN_SYMBOL", r.message or f"No instrument matches '{symbol}'.")
    inst = r.instrument
    w = s.execute(select(WhatsAppWatch).where(WhatsAppWatch.user_id == user_id, WhatsAppWatch.instrument_id == inst.instrument_id)).scalar_one_or_none()
    if w is None:
        n = s.execute(select(func.count()).select_from(WhatsAppWatch).where(WhatsAppWatch.user_id == user_id)).scalar_one()
        if n >= wa.MAX_WATCHES:
            raise _err(409, "WATCH_LIMIT", f"You can have at most {wa.MAX_WATCHES} watches. Remove one first.")
        w = WhatsAppWatch(user_id=user_id, instrument_id=inst.instrument_id, symbol=inst.symbol)
        s.add(w)
    w.interval_min, w.min_move_pct, w.market_hours_only, w.active = interval_min, min_move_pct, market_hours_only, True
    s.flush()
    return w


@router.post("/watches")
def create_watch(body: WatchBody, p: Principal = Depends(current)):
    with session_scope() as s:
        w = add_watch(s, p.user_id, body.symbol, body.interval_min, body.min_move_pct, body.market_hours_only)
        s.refresh(w)
        return _watch_view(w)


def _own_watch(s: Session, wid: str, user_id: str) -> WhatsAppWatch:
    w = s.get(WhatsAppWatch, wid)
    if w is None or w.user_id != user_id:
        raise _err(404, "NOT_FOUND", "No such watch.")
    return w


@router.delete("/watches/{wid}")
def delete_watch(wid: str, p: Principal = Depends(current)):
    with session_scope() as s:
        s.delete(_own_watch(s, wid, p.user_id))
    return {"deleted": True}


class WatchPatch(BaseModel):
    active: bool


@router.patch("/watches/{wid}")
def patch_watch(wid: str, body: WatchPatch, p: Principal = Depends(current)):
    with session_scope() as s:
        w = _own_watch(s, wid, p.user_id)
        w.active = body.active
        s.flush()
        return _watch_view(w)


class PauseBody(BaseModel):
    minutes: int


@router.post("/pause")
def pause(body: PauseBody, p: Principal = Depends(current)):
    """Holds back alerts and watches until paused_until (1 minute to 7 days). Held-back alerts are listed as PAUSED."""
    if not (1 <= body.minutes <= 7 * 24 * 60):
        raise _err(422, "BAD_MINUTES", "Pause for 1 minute to 7 days.")
    with session_scope() as s:
        lk = s.get(WhatsAppLink, p.user_id)
        if lk is None:
            raise _err(409, "NOT_LINKED", "Link a WhatsApp number first.")
        lk.paused_until = datetime.now(UTC) + timedelta(minutes=body.minutes)
        s.flush()
        return {"paused_until": iso(lk.paused_until), "link": _link_view(lk)}


@router.post("/resume")
def resume(p: Principal = Depends(current)):
    with session_scope() as s:
        lk = s.get(WhatsAppLink, p.user_id)
        if lk is None:
            raise _err(409, "NOT_LINKED", "Link a WhatsApp number first.")
        lk.paused_until = None
        s.flush()
        return {"paused_until": None, "link": _link_view(lk)}


@router.post("/test")
def test_message(p: Principal = Depends(current)):
    with session_scope() as s:
        lk = s.get(WhatsAppLink, p.user_id)
        if lk is None or lk.verified_at is None:
            raise _err(409, "NOT_VERIFIED", "Link and verify your WhatsApp number first.")
    return wa.send_via_notifier(p.user_id, "test", "*Countersign test message*\nYour WhatsApp alerts are working.")


# ------------------------------------------------------------------ inbound (Twilio webhook): read-only commands

TRADING_VERBS = {
    "BUY", "SELL", "CANCEL", "APPROVE", "CONFIRM", "MODIFY", "PLACE", "EXIT", "SQUAREOFF", "SQUARE", "SHORT", "COVER",
    "ORDER", "TRADE", "EXECUTE", "SIGN", "COUNTERSIGN", "ACCEPT", "REJECT", "YES", "OK", "ARM", "DISARM", "KILL",
}
HELP = (
    "*Countersign commands*\n\n"
    "*Your account*\n"
    "- *PNL*: today's and total P&L\n"
    "- *HOLDINGS*: your holdings\n"
    "- *POSITIONS*: open positions\n"
    "- *ORDERS*: today's orders\n"
    "- *MANDATES*: your standing instructions\n\n"
    "*Prices*\n"
    "- *PRICE ITC*: live price\n"
    "- *WATCH TCS 5*: an update every 5 minutes (2, 5, 10, 15 or 30)\n"
    "- *WATCH TCS 5 1%*: only when it moves 1% or more\n"
    "- *UNWATCH TCS*: stop a watch\n"
    "- *WATCHES*: list your watches\n\n"
    "*Alerts*\n"
    "- *PAUSE 2H* and *RESUME*\n"
    "- *STOP* and *START*\n\n"
    "_WhatsApp is read only. Orders and approvals happen only in the app._"
)


def refusal() -> str:
    return (
        "*WhatsApp can't place or approve orders*\n\n"
        f"For your safety, trades are drafted and countersigned only in the app:\n{wa.public_app_url()}/copilot"
    )


def not_linked() -> str:
    return (
        "*This number is not linked to Countersign*\n\n"
        f"1. Open {wa.public_app_url()}\n"
        "2. Go to *WhatsApp alerts* in the menu\n"
        "3. Enter this number and the code we send you"
    )


def _broker_down() -> str:
    return "*Broker not reachable*\nCould not reach your broker account right now. Try again in a minute."


def _acct(s: Session, user_id: str) -> str | None:
    from countersign.broker021.linking import active_account

    u = s.get(User, user_id)
    a = active_account(s, u) if u else None
    return a.id if a else None


def _cmd_pnl(s: Session, uid: str) -> str:
    acct = _acct(s, uid)
    if acct is None:
        return "*No trading account*\nNo trading account is linked to your Countersign user."
    m = portfolio.summary(s, read_gateway(acct), acct)
    return (
        "*Your P&L*\n\n"
        f"- Today: *{inr(m['day_pnl_paise'], signed=True)}*\n"
        f"- Total: *{inr(m['total_pnl_paise'], signed=True)}*\n"
        f"- Holdings: {inr(m['holdings_value_paise'])} ({m['holdings_count']} stocks)\n"
        f"- Positions: {m['positions_count']} · Open orders: {m['open_orders_count']}\n"
        f"- Available: {inr(m['available_margin_paise'])}"
    )


def _cmd_holdings(s: Session, uid: str) -> str:
    acct = _acct(s, uid)
    rows = portfolio.holdings(s, read_gateway(acct)) if acct else []
    if not rows:
        return "*Holdings*\nYou have no holdings right now."
    lines = [
        f"*{h['instrument']['symbol']}* · {h['quantity']} shares\nAvg {inr(h['avg_price_paise'])} · LTP {inr(h['ltp_paise'])} · P&L *{inr(h['pnl_paise'], signed=True)}*"
        for h in rows[:10]
    ]
    more = f"\n\n_+{len(rows) - 10} more in the app._" if len(rows) > 10 else ""
    return f"*Holdings* ({len(rows)})\n\n" + "\n\n".join(lines) + more


def _cmd_positions(s: Session, uid: str) -> str:
    acct = _acct(s, uid)
    rows = [r for r in (portfolio.positions(s, read_gateway(acct)) if acct else []) if r["quantity"] != 0]
    if not rows:
        return "*Positions*\nNo open positions."
    lines = [
        f"*{r['instrument']['symbol']}* · {r['quantity']} {r['product']}\nAvg {inr(r['avg_price_paise'])} · LTP {inr(r['ltp_paise'])} · P&L *{inr(r['pnl_paise'], signed=True)}*"
        for r in rows[:10]
    ]
    return f"*Positions* ({len(rows)})\n\n" + "\n\n".join(lines)


def _cmd_orders(s: Session, uid: str) -> str:
    acct = _acct(s, uid)
    rows = portfolio.orders(s, read_gateway(acct)) if acct else []
    if not rows:
        return "*Orders*\nNo orders today."
    rows = sorted(rows, key=lambda o: str(o.get("updated_at") or o.get("placed_at") or ""), reverse=True)[:8]
    out = []
    for o in rows:
        px = f" @ {inr(o.get('limit_price_paise'))}" if o.get("limit_price_paise") else " at market"
        status = ORDER_STATUS_WORDS.get(str(o.get("status")), str(o.get("status")).replace("_", " ").title())
        fill = f" {o.get('filled_quantity')}/{o.get('quantity')}" if o.get("filled_quantity") else ""
        out.append(f"- {o.get('side')} {o.get('quantity')} *{o['instrument']['symbol']}*{px} · {status}{fill}")
    return f"*Today's orders* (latest {len(rows)})\n\n" + "\n".join(out)


ORDER_STATUS_WORDS = {
    "COMPLETE": "Filled",
    "OPEN": "Open",
    "PARTIALLY_FILLED": "Partly filled",
    "TRIGGER_PENDING": "Waiting for trigger",
    "REJECTED": "Rejected",
    "CANCELLED": "Cancelled",
}


def _cmd_price(s: Session, arg: str) -> str:
    if not arg:
        return "Send *PRICE <SYMBOL>*, for example *PRICE RELIANCE*."
    r = instruments.resolve(s, arg[:40])
    if r.status != "RESOLVED" or r.instrument is None:
        return r.message or f"No instrument matches '{arg}'."
    t = market.latest(r.instrument.instrument_id)
    if t is None:
        return f"No live price for *{r.instrument.symbol}* right now."
    day = (t.ltp_paise - t.prev_close_paise) * 100 / t.prev_close_paise if t.prev_close_paise else 0.0
    return f"*{r.instrument.symbol}* {inr(t.ltp_paise)}\n{wa.pct(day)} today"


def _cmd_mandates(s: Session, uid: str) -> str:
    rows = s.execute(select(StandingMandate).where(StandingMandate.user_id == uid, StandingMandate.state.in_(["ACTIVE", "NEEDS_REVIEW", "TRIGGERED", "EXECUTING"])).order_by(StandingMandate.created_at.desc()).limit(10)).scalars().all()
    if not rows:
        return "*Standing instructions*\nYou have no active standing instructions."
    from countersign.notifier.service import mandate_summary

    lines = []
    for i, m in enumerate(rows, 1):
        sentence, meta = mandate_summary(m)
        state = "" if m.state == "ACTIVE" else f"_{m.state.replace('_', ' ').capitalize()}_ · "
        lines.append(f"{i}. {sentence}\n    {state}{meta}")
    return (
        f"*Your standing instructions* ({len(rows)})\n\n" + "\n\n".join(lines)
        + "\n\n_To cancel one, open the app. It needs your passkey or PIN._"
    )


def _cmd_watch(s: Session, uid: str, args: list[str]) -> str:
    if len(args) < 2 or not args[1].isdigit():
        return "Send *WATCH <SYMBOL> <MINUTES>*, for example *WATCH RELIANCE 5*, or *WATCH RELIANCE 5 0.5%* for moves of 0.5% or more."
    mm = None
    if len(args) >= 3:
        try:
            mm = float(args[2].rstrip("%"))
        except ValueError:
            return "The minimum move must be a number, for example *0.5%*."
    try:
        w = add_watch(s, uid, args[0], int(args[1]), mm, False)
    except HTTPException as e:
        return (e.detail or {}).get("message", "Could not add that watch.") if isinstance(e.detail, dict) else "Could not add that watch."
    extra = f", only when it moves {mm:g}% or more" if mm else ""
    return f"*Watching {w.symbol}*\nAn update every {w.interval_min} minutes{extra}.\nSend *UNWATCH {w.symbol}* to stop."


def _cmd_unwatch(s: Session, uid: str, arg: str) -> str:
    if not arg:
        return "Send *UNWATCH <SYMBOL>*, for example *UNWATCH RELIANCE*."
    sym = instruments.normalize_symbol(arg)
    rows = s.execute(select(WhatsAppWatch).where(WhatsAppWatch.user_id == uid, WhatsAppWatch.symbol == sym)).scalars().all()
    if not rows:
        return f"You are not watching *{sym}*."
    for w in rows:
        s.delete(w)
    return f"*Watch stopped*\nNo more updates for {sym}."


def _cmd_watches(s: Session, uid: str) -> str:
    rows = s.execute(select(WhatsAppWatch).where(WhatsAppWatch.user_id == uid).order_by(WhatsAppWatch.created_at)).scalars().all()
    if not rows:
        return "*Price watches*\nNo watches yet. Send *WATCH TCS 5* to add one."
    lines = [
        f"- *{w.symbol}* every {w.interval_min} min" + (f", on a {w.min_move_pct:g}% move" if w.min_move_pct else "") + ("" if w.active else " _(paused)_")
        for w in rows
    ]
    return f"*Your price watches* ({len(rows)})\n\n" + "\n".join(lines)


def handle_command(s: Session, link: WhatsAppLink, text: str, now: datetime | None = None) -> str:
    """The reply to one inbound message. Read-only: no path here reaches an order, an approval or a cancellation."""
    now = now or datetime.now(UTC)
    words = re.sub(r"\s+", " ", (text or "").strip()).split(" ")
    verb = re.sub(r"[^A-Z]", "", words[0].upper()) if words and words[0] else ""
    args = [w for w in words[1:] if w]
    uid = link.user_id
    if verb in TRADING_VERBS:
        return refusal()
    if verb in ("", "HELP", "HI", "HELLO", "MENU", "COMMANDS"):
        return HELP
    if verb == "STOP":
        link.opted_in = False
        return "*Alerts stopped*\nYou will get no more messages from Countersign.\nSend *START* to turn them back on."
    if verb == "START":
        link.opted_in = True
        return "*Alerts are back on*\nSend *HELP* for the commands."
    if verb == "PAUSE":
        m = re.fullmatch(r"(\d{1,2})H?", (args[0].upper() if args else ""))
        if not m or not (1 <= int(m.group(1)) <= 72):
            return "Send *PAUSE <N>H* with N from 1 to 72, for example *PAUSE 2H*."
        link.paused_until = now + timedelta(hours=int(m.group(1)))
        return f"*Alerts paused for {int(m.group(1))} hours*\nSend *RESUME* to turn them back on sooner."
    if verb == "RESUME":
        link.paused_until = None
        return "*Alerts resumed*\nYou will get alerts again."
    if not link.opted_in and verb not in ("HELP",):
        return "*Alerts are off for this number*\nSend *START* to turn them back on."
    try:
        if verb == "PNL":
            return _cmd_pnl(s, uid)
        if verb == "HOLDINGS":
            return _cmd_holdings(s, uid)
        if verb == "POSITIONS":
            return _cmd_positions(s, uid)
        if verb == "ORDERS":
            return _cmd_orders(s, uid)
    except (BrokerError, BrokerTimeout, BrokerUnavailable):
        return _broker_down()
    if verb in ("PRICE", "QUOTE", "LTP"):
        return _cmd_price(s, " ".join(args))
    if verb in ("MANDATES", "INSTRUCTIONS"):
        return _cmd_mandates(s, uid)
    if verb == "WATCH":
        return _cmd_watch(s, uid, args)
    if verb == "UNWATCH":
        return _cmd_unwatch(s, uid, " ".join(args))
    if verb == "WATCHES":
        return _cmd_watches(s, uid)
    return "Unknown command. Send HELP for the list. WhatsApp is read-only: orders and approvals happen in the app."


def twiml(message: str) -> Response:
    msg = message if len(message) <= 1500 else message[:1497] + "..."
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(msg)}</Message></Response>', media_type="application/xml")


@router.post("/inbound")
async def inbound(request: Request):
    """Twilio webhook (no session, no CSRF: Twilio's signature is the authentication, checked by the notifier)."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        code, data = wa.notifier_call("POST", "/v1/inbound/verify", {"url": settings().whatsapp_public_url, "body": raw, "signature": request.headers.get("x-twilio-signature")})
    except wa.NotifierDown:
        return Response("notifier unavailable", status_code=503, media_type="text/plain")
    if code == 403:
        return Response("invalid signature", status_code=403, media_type="text/plain")
    if code != 200 or not data.get("valid"):
        return Response("webhook not available", status_code=503 if code == 503 else 403, media_type="text/plain")
    form = dict(parse_qsl(raw, keep_blank_values=True))
    sender = wa.normalize_phone(str(form.get("From", "")).replace("whatsapp:", ""))
    text = str(form.get("Body", ""))[:500]
    now = datetime.now(UTC)
    with session_scope() as s:
        link = s.execute(select(WhatsAppLink).where(WhatsAppLink.phone_e164 == sender, WhatsAppLink.verified_at.is_not(None))).scalar_one_or_none() if sender else None
        if link is None:
            wa.log_message(s, user_id=None, kind="inbound", phone=sender, body=text, status="UNKNOWN_SENDER", direction="in")
            return twiml(not_linked())
        link.last_inbound_at = now
        verb = (text.strip().split(" ")[0] if text.strip() else "").upper()[:16]
        wa.log_message(s, user_id=link.user_id, kind="inbound", phone=link.phone_e164, body=text, status="RECEIVED", direction="in")
        reply = handle_command(s, link, text, now)
        # The reply travels in the webhook response (TwiML), so Twilio sends it: logged as SENT.
        wa.log_message(s, user_id=link.user_id, kind=("reply_" + re.sub(r"[^a-z]", "", verb.lower()))[:24], phone=link.phone_e164, body=reply, status="SENT")
    return twiml(reply)

