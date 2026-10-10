"""WhatsApp alerts: linking and verification, log-only mode, Twilio errors, webhook signatures, read-only commands,
alert routing from the app's events, price watches, rate limits, and the step-up now needed to cancel a mandate.
Twilio is an httpx.MockTransport; the notifier app is reached in-process exactly as the API reaches it over loopback."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from countersign.core import proposals, state
from countersign.core import whatsapp as wa
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.mandate_engine import evaluate
from countersign.core.market import Tick
from countersign.core.models import StandingMandate, WhatsAppLink, WhatsAppMessage, WhatsAppPrefs, WhatsAppWatch
from countersign.core.parser import parse
from countersign.notifier import main as notifier_main
from countersign.notifier import service, twilio
from countersign.notifier.settings import TwilioSettings
from tests.accounts_support import account_of, csrf, new_client, signed_up
from tests.conftest import FakeGateway, set_tick
from tests.helpers import approve, lab_user_id

PHONE = "+919876543210"
TOKEN = "test-auth-token"
LIVE = TwilioSettings(_env_file=None, account_sid="ACtest", auth_token=TOKEN, whatsapp_from="whatsapp:+14155238886")
LOG_ONLY = TwilioSettings(_env_file=None)
PIN = "7391"


class FakeTwilio:
    """Records every Twilio request. next_error: a Twilio error code for the next send; status: the read-back."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.next_error: int | None = None
        self.read_back = {"status": "delivered", "error_code": None}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "POST":
            form = dict(parse_qsl(request.content.decode()))
            if self.next_error is not None:
                code, self.next_error = self.next_error, None
                return httpx.Response(400, json={"code": code, "message": "error", "status": 400})
            self.sent.append(form)
            return httpx.Response(201, json={"sid": f"SM{len(self.sent):032d}", "status": "queued", "error_code": None})
        return httpx.Response(200, json={"sid": request.url.path.rsplit("/", 1)[-1].removesuffix(".json"), **self.read_back})

    def last_code(self) -> str:
        return re.search(r"\b(\d{6})\b", self.sent[-1]["Body"]).group(1)


@pytest.fixture
def tw(monkeypatch):
    fake = FakeTwilio()
    twilio.TRANSPORT[:] = [httpx.MockTransport(fake)]
    service.CONFIG[:] = [LIVE]
    wa._client[:] = [TestClient(notifier_main.app, base_url="http://notifier")]
    yield fake
    twilio.TRANSPORT.clear()
    service.CONFIG.clear()
    wa._client.clear()


def linked(main, tw: FakeTwilio, phone: str = PHONE):
    c, me = signed_up(main)
    r = c.post("/api/whatsapp/link", json={"phone": phone}, headers=csrf(c))
    assert r.status_code == 200, r.text
    r = c.post("/api/whatsapp/verify", json={"code": tw.last_code()}, headers=csrf(c))
    assert r.status_code == 200, r.text
    return c, me["user"]["id"]


def link_directly(user_id: str, phone: str = PHONE) -> None:
    with session_scope() as s:
        s.add(WhatsAppLink(user_id=user_id, phone_e164=phone, verified_at=datetime.now(UTC), opted_in=True))
        wa.prefs_of(s, user_id)


def inbound(main, body: str, phone: str = PHONE, signature: str | None = "auto"):
    params = [("SmsMessageSid", "SM1"), ("From", "whatsapp:" + phone), ("To", "whatsapp:+14155238886"), ("Body", body), ("NumMedia", "0")]
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signature == "auto":
        signature = twilio.signature(TOKEN, settings().whatsapp_public_url, params)
    if signature is not None:
        headers["X-Twilio-Signature"] = signature
    return new_client(main).post("/api/whatsapp/inbound", content=urlencode(params), headers=headers)


def reply_of(r) -> str:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    m = re.search(r"<Message>(.*)</Message>", r.text, re.S)
    return m.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


# ------------------------------------------------------------------ helpers


def test_phone_validation_and_masking():
    assert wa.normalize_phone("+91 98765 43210") == PHONE
    assert wa.normalize_phone("0091-98765-43210") == PHONE
    assert wa.normalize_phone("+91 12345 67890") is None  # Indian mobiles start with 6 to 9
    assert wa.normalize_phone("9876543210") is None  # no country code
    assert wa.normalize_phone("+1 415 523 8886") == "+14155238886"
    assert wa.mask_phone(PHONE) == "+91******3210"
    assert wa.preview("Your Countersign verification code is 123456. It expires in 10 minutes.").count("123456") == 0


# ------------------------------------------------------------------ link and verify


def test_link_and_verify_sends_code_then_welcome(api, tw):
    main, _ = api
    c, me = signed_up(main)
    bad = c.post("/api/whatsapp/link", json={"phone": "12345"}, headers=csrf(c))
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_PHONE"
    r = c.post("/api/whatsapp/link", json={"phone": "+91 98765 43210"}, headers=csrf(c))
    assert r.status_code == 200, r.text
    assert r.json()["phone_masked"] == "+91******3210" and r.json()["delivery"]["status"] == "SENT"
    assert tw.sent[-1]["To"] == "whatsapp:" + PHONE and tw.sent[-1]["From"] == "whatsapp:+14155238886"
    assert tw.sent[-1]["Body"].startswith("Your Countersign verification code is ") and tw.sent[-1]["Body"].endswith("It expires in 10 minutes.")
    st = c.get("/api/whatsapp").json()
    assert st["link"] == {"phone_masked": "+91******3210", "verified": False, "opted_in": True, "paused_until": None, "window_hint": None}
    assert st["mode"] == "live" and st["configured"] is True and st["sandbox"]["number"] == "+1 415 523 8886"
    v = c.post("/api/whatsapp/verify", json={"code": tw.last_code()}, headers=csrf(c))
    assert v.status_code == 200 and v.json()["verified"] is True
    assert "can never approve" in tw.sent[-1]["Body"] and "PNL" in tw.sent[-1]["Body"]
    st = c.get("/api/whatsapp").json()
    assert st["link"]["verified"] is True and st["prefs"] == {"orders": True, "mandates": True, "security": True, "digests": False}
    assert [m["kind"] for m in st["recent"]][:2] == ["welcome", "verify"]
    assert all(PHONE not in json.dumps(m) for m in st["recent"])
    with session_scope() as s:
        stored = s.execute(select(WhatsAppMessage.preview).where(WhatsAppMessage.kind == "verify")).scalar_one()
        assert not re.search(r"\d{6}", stored)  # the code is never stored in the log


def test_wrong_code_attempts_and_expiry(api, tw):
    main, _ = api
    c, _ = signed_up(main)
    c.post("/api/whatsapp/link", json={"phone": PHONE}, headers=csrf(c))
    good = tw.last_code()
    wrong = "000000" if good != "000000" else "111111"
    for left in (4, 3, 2, 1):
        r = c.post("/api/whatsapp/verify", json={"code": wrong}, headers=csrf(c))
        assert r.status_code == 401 and r.json()["attempts_left"] == left
    r = c.post("/api/whatsapp/verify", json={"code": wrong}, headers=csrf(c))
    assert r.status_code == 401 and r.json()["attempts_left"] == 0
    r = c.post("/api/whatsapp/verify", json={"code": good}, headers=csrf(c))
    assert r.status_code == 404 and r.json()["code"] == "NO_CODE"  # the code is spent after 5 wrong attempts
    c.post("/api/whatsapp/link", json={"phone": PHONE}, headers=csrf(c))
    with session_scope() as s:
        s.execute(text("UPDATE whatsapp_codes SET expires_at = now() - interval '1 minute'"))
    r = c.post("/api/whatsapp/verify", json={"code": tw.last_code()}, headers=csrf(c))
    assert r.status_code == 410 and r.json()["code"] == "CODE_EXPIRED"
    c.post("/api/whatsapp/link", json={"phone": PHONE}, headers=csrf(c))
    r = c.post("/api/whatsapp/link", json={"phone": PHONE}, headers=csrf(c))
    assert r.status_code == 429  # at most 3 codes per 10 minutes


def test_log_only_mode_records_without_sending(api, tw, capsys):
    main, _ = api
    service.CONFIG[:] = [LOG_ONLY]
    c, _ = signed_up(main)
    r = c.post("/api/whatsapp/link", json={"phone": PHONE}, headers=csrf(c))
    assert r.json()["delivery"]["status"] == "LOGGED"
    assert tw.sent == []
    out = capsys.readouterr().out
    assert "+91******3210" in out and PHONE not in out
    code = re.search(r"code is (\d{6})", out).group(1)
    assert c.post("/api/whatsapp/verify", json={"code": code}, headers=csrf(c)).json()["welcome"]["status"] == "LOGGED"
    st = c.get("/api/whatsapp").json()
    assert st["mode"] == "log_only" and st["configured"] is False
    assert {m["status"] for m in st["recent"]} == {"LOGGED"}
    assert c.post("/api/whatsapp/test", headers=csrf(c)).json()["status"] == "LOGGED"
    # Without the auth token no webhook signature can be checked, so inbound is refused.
    assert inbound(main, "HELP").status_code == 503


def test_twilio_window_error_is_explained_and_never_retried(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    n = len(tw.sent)
    tw.next_error = 63016
    r = c.post("/api/whatsapp/test", headers=csrf(c))
    assert r.json()["status"] == "FAILED_WINDOW" and "reopen the 24-hour window" in r.json()["hint"] and r.json()["error_code"] == 63016
    assert len(tw.sent) == n  # one attempt, no retry
    st = c.get("/api/whatsapp").json()
    assert st["recent"][0]["status"] == "FAILED_WINDOW" and "send any message to the sandbox number".lower() in st["link"]["window_hint"].lower()
    # A message from the trader reopens the window.
    reply_of(inbound(main, "HELP"))
    assert c.get("/api/whatsapp").json()["link"]["window_hint"] is None
    tw.next_error = 63015
    r = c.post("/api/whatsapp/test", headers=csrf(c)).json()
    assert r["status"] == "FAILED" and r["error_code"] == 63015 and "join" in r["hint"]
    assert {m["status"] for m in c.get("/api/whatsapp").json()["recent"]} <= {"SENT", "LOGGED", "FAILED", "FAILED_WINDOW", "PAUSED"}


def test_delivery_status_read_back_catches_async_63016(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    tw.read_back = {"status": "failed", "error_code": 63016}
    with session_scope() as s:
        assert service.refresh_statuses(s) >= 1
    st = c.get("/api/whatsapp").json()
    assert st["recent"][0]["status"] == "FAILED_WINDOW" and st["link"]["window_hint"]


def test_unlink_prefs_and_test_message(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    r = c.put("/api/whatsapp/prefs", json={"orders": False, "mandates": True, "security": True, "digests": True}, headers=csrf(c))
    assert r.json() == {"orders": False, "mandates": True, "security": True, "digests": True}
    assert c.post("/api/whatsapp/test", headers=csrf(c)).json()["status"] == "SENT"
    assert tw.sent[-1]["Body"] == "Countersign test message: alerts are working."
    assert c.put("/api/whatsapp/prefs", json={"orders": True}, headers={}).status_code == 403  # CSRF still applies
    assert c.delete("/api/whatsapp/link", headers=csrf(c)).json() == {"unlinked": True}
    assert c.get("/api/whatsapp").json()["link"] is None
    assert c.post("/api/whatsapp/test", headers=csrf(c)).status_code == 409


# ------------------------------------------------------------------ webhook signature


def test_signature_valid_invalid_missing(api, tw):
    main, _ = api
    linked(main, tw)
    assert "PNL" in reply_of(inbound(main, "HELP"))
    assert inbound(main, "HELP", signature="bm90IHRoZSBzaWduYXR1cmU=").status_code == 403
    assert inbound(main, "HELP", signature=None).status_code == 403
    # The signature covers the URL too: a signature for another URL is refused.
    params = [("From", "whatsapp:" + PHONE), ("Body", "HELP")]
    other = twilio.signature(TOKEN, "https://evil.example/api/whatsapp/inbound", params)
    r = new_client(main).post("/api/whatsapp/inbound", content=urlencode(params), headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": other})
    assert r.status_code == 403


def test_signature_algorithm_matches_twilio_reference():
    # The reference vector from Twilio's request validator tests (auth token 12345).
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = [("Digits", "1234"), ("To", "+18005551212"), ("From", "+14158675309"), ("Caller", "+14158675309"), ("CallSid", "CA1234567890ABCDE")]
    assert twilio.signature("12345", url, params) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_notifier_needs_its_key(tw):
    c = TestClient(notifier_main.app)
    assert c.post("/v1/send", json={"user_id": "x", "kind": "test", "body": "hi"}).status_code == 401
    assert c.post("/v1/send", json={"user_id": "x", "kind": "test", "body": "hi"}, headers={"X-Notifier-Key": "wrong"}).status_code == 401
    h = c.get("/health").json()
    assert h["mode"] == "live" and TOKEN not in json.dumps(h) and "ACtest" not in json.dumps(h)


# ------------------------------------------------------------------ inbound commands


def test_unknown_sender_gets_link_instructions(api, tw):
    main, _ = api
    linked(main, tw)
    msg = reply_of(inbound(main, "PNL", phone="+919811112222"))
    assert "not linked to Countersign" in msg


@pytest.mark.parametrize("verb", ["BUY 10 RELIANCE", "sell infy", "CANCEL all", "approve", "Yes", "modify order"])
def test_trading_verbs_are_refused(api, tw, verb):
    main, _ = api
    linked(main, tw)
    msg = reply_of(inbound(main, verb))
    assert msg == "WhatsApp can't place or approve orders. Open Countersign to draft and countersign: https://countersign.everyrepo.me/copilot"


def test_read_only_commands(api, tw, monkeypatch):
    main, _ = api
    c, uid = linked(main, tw)
    from countersign.api import whatsapp as wa_api

    monkeypatch.setattr(wa_api, "read_gateway", lambda acct: FakeGateway())
    set_tick("NSE:EQ:INFY", 186000, prev=185000)
    set_tick("NSE:EQ:RELIANCE", 120760, prev=121245)
    assert "PNL" in reply_of(inbound(main, "help"))
    assert "P&L today" in reply_of(inbound(main, "PNL"))
    h = reply_of(inbound(main, "HOLDINGS"))
    assert "INFY 100 @ ₹1,820.40" in h and "LTP ₹1,860.00" in h
    assert reply_of(inbound(main, "POSITIONS")) == "No open positions."
    assert reply_of(inbound(main, "ORDERS")) == "No orders today."
    assert reply_of(inbound(main, "PRICE RELIANCE")) == "RELIANCE ₹1,207.60 (−0.4% today)"
    assert reply_of(inbound(main, "MANDATES")) == "No active standing instructions."
    assert "Watching RELIANCE every 5 minutes, only when it moves 0.5% or more" in reply_of(inbound(main, "WATCH RELIANCE 5 0.5%"))
    assert "2, 5, 10, 15 or 30" in reply_of(inbound(main, "WATCH RELIANCE 7"))
    assert "No instrument" in reply_of(inbound(main, "WATCH NOSUCHSYMBOLX 5")) or "match" in reply_of(inbound(main, "WATCH NOSUCHSYMBOLX 5"))
    assert reply_of(inbound(main, "WATCHES")) == "Watches:\nRELIANCE every 5 min, min move 0.5%"
    assert c.get("/api/whatsapp").json()["watches"][0]["symbol"] == "RELIANCE"
    assert reply_of(inbound(main, "UNWATCH RELIANCE")) == "Stopped watching RELIANCE."
    assert "No watches" in reply_of(inbound(main, "WATCHES"))
    assert "paused for 2 hours" in reply_of(inbound(main, "PAUSE 2H"))
    assert c.get("/api/whatsapp").json()["link"]["paused_until"] is not None
    assert reply_of(inbound(main, "RESUME")) == "Alerts resumed."
    assert c.get("/api/whatsapp").json()["link"]["paused_until"] is None
    assert "Alerts stopped" in reply_of(inbound(main, "STOP"))
    assert c.get("/api/whatsapp").json()["link"]["opted_in"] is False
    assert c.post("/api/whatsapp/test", headers=csrf(c)).json()["status"] == "OPTED_OUT"
    assert "Send START" in reply_of(inbound(main, "PNL"))
    assert "back on" in reply_of(inbound(main, "START"))
    assert "Unknown command" in reply_of(inbound(main, "FOO"))
    with session_scope() as s:
        kinds = {m.direction for m in s.execute(select(WhatsAppMessage).where(WhatsAppMessage.user_id == uid)).scalars()}
        assert kinds == {"in", "out"}


# ------------------------------------------------------------------ alerts from events


def _lab_mandate(account) -> tuple[str, str]:
    set_tick("NSE:EQ:INFY", 186000)
    text_ = "Buy 10 INFY if the price falls below 1800"
    with session_scope() as s:
        out = proposals.create_mandate(s, parse(text_).mandate, user_text=text_, account=account, user_id=lab_user_id(), engine="deterministic", origin="test")
        pid = out.proposal.id
    approve(pid)
    with session_scope() as s:
        return s.execute(select(StandingMandate.id).where(StandingMandate.proposal_id == pid)).scalar_one(), pid


def _pump() -> int:
    with session_scope() as s:
        return service.pump_once(s)


def _bodies(uid: str) -> list[str]:
    with session_scope() as s:
        return [m.preview for m in s.execute(select(WhatsAppMessage).where(WhatsAppMessage.user_id == uid).order_by(WhatsAppMessage.ts)).scalars()]


def test_mandate_armed_triggered_filled_and_cancelled_notifications(tw, account):
    uid = lab_user_id()
    link_directly(uid)
    _pump()  # places the cursor
    mid, pid = _lab_mandate(account)
    assert _pump() == 1
    body = tw.sent[-1]["Body"]
    assert body.startswith("Armed: if INFY falls to ₹1,800.00 or below, buy 10 (limit ₹")
    assert "Fires once. Expires " in body
    assert _pump() == 0 and len(tw.sent) == 1  # exactly once
    with session_scope() as s:
        m = s.get(StandingMandate, mid)
        tick = Tick(instrument_id="NSE:EQ:INFY", symbol="INFY", ltp_paise=179500, bid_paise=179495, ask_paise=179505, volume=1, prev_close_paise=186000, ts=datetime.now(UTC), seq=5)
        assert evaluate(s, m, tick, gateway=FakeGateway()) == "TRIGGERED"
    _pump()
    assert tw.sent[-1]["Body"].startswith("Triggered: INFY at ₹1,795.00.")
    with session_scope() as s:
        state.outcome(s, "ORDER_FILLED", "Filled: BUY 10 INFY", "Average ₹1,795.00 (order B000001).", "success", proposal_id=pid, account_id="LAB1")
        state.outcome(s, "ORDER_REJECTED", "Rejected: BUY 10 INFY", "RMS: insufficient funds", "danger", proposal_id=pid, account_id="LAB1")
    _pump()
    assert tw.sent[-2]["Body"] == "Standing instruction: Filled: BUY 10 INFY. Average ₹1,795.00 (order B000001)."
    assert tw.sent[-1]["Body"] == "Standing instruction: Rejected: BUY 10 INFY. RMS: insufficient funds"
    # Expired and cancelled come from the audit chain.
    mid2, _ = _lab_mandate(account)
    with session_scope() as s:
        s.execute(text("UPDATE mandates SET state='CANCELLED' WHERE id=:id"), {"id": mid2})
        from countersign.core import audit

        audit.record(s, "trader", "MANDATE_CANCELLED", "mandate", mid2, {})
    _pump()
    assert tw.sent[-1]["Body"] == "Cancelled: your standing instruction on INFY was cancelled and can no longer fire."


def test_prefs_pause_and_opt_out_filter_alerts(tw, account):
    uid = lab_user_id()
    link_directly(uid)
    _pump()
    with session_scope() as s:
        s.get(WhatsAppPrefs, uid).orders = False
        state.outcome(s, "ORDER_FILLED", "Filled: BUY 5 INFY", "Average ₹1,860.00.", "success", account_id="LAB1")
        state.outcome(s, "KILL_SWITCH", "Kill switch engaged", "Trader engaged it", "danger", account_id="*")
    _pump()
    assert [b["Body"][:40] for b in tw.sent] == ["Security: kill switch engaged. Countersi"]
    with session_scope() as s:
        s.get(WhatsAppLink, uid).paused_until = datetime.now(UTC) + timedelta(hours=1)
        state.outcome(s, "KILL_SWITCH", "Kill switch released", "ok", "info", account_id="*")
    _pump()
    assert len(tw.sent) == 1
    with session_scope() as s:
        held = s.execute(select(WhatsAppMessage.status).where(WhatsAppMessage.user_id == uid, WhatsAppMessage.kind == "kill_switch").order_by(WhatsAppMessage.ts)).scalars().all()
        assert held == ["SENT", "PAUSED"]  # held back while paused, recorded once, never sent later
    _pump()
    assert len(tw.sent) == 1
    with session_scope() as s:
        lk = s.get(WhatsAppLink, uid)
        lk.paused_until, lk.opted_in = None, False
        state.outcome(s, "KILL_SWITCH", "Kill switch engaged", "again", "danger", account_id="*")
    _pump()
    assert len(tw.sent) == 1


def test_security_events_and_sign_in(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    _pump()
    assert c.post("/api/auth/pin", json={"pin": PIN}, headers=csrf(c)).status_code == 200
    assert c.post("/api/auth/pin", json={"pin": "8264", "current_pin": PIN}, headers=csrf(c)).status_code == 200
    with session_scope() as s:
        from countersign.core import audit
        from countersign.core.models import SessionRow

        sid = s.execute(select(SessionRow.id).where(SessionRow.user_id == uid)).scalars().first()
        s.execute(text("UPDATE sessions SET user_agent='Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36' WHERE id=:i"), {"i": sid})
        audit.record(s, "trader", "SESSION_CREATED", "session", sid, {"method": "password"})
    _pump()
    bodies = [b["Body"] for b in tw.sent]
    assert any(b.startswith("Security: ") and "PIN" in b for b in bodies)
    assert any("new sign-in to Countersign (Chrome on Windows, password)" in b for b in bodies)
    assert sum("new sign-in" in b for b in bodies) == 1  # the sign-up session predates the cursor: never replayed


def test_rate_limit_per_user(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    statuses = [c.post("/api/whatsapp/test", headers=csrf(c)).json()["status"] for _ in range(4)]
    assert statuses == ["SENT"] * 4
    with session_scope() as s:
        for i in range(wa.USER_LIMIT[0]):
            wa.log_message(s, user_id=uid, kind="test", phone=PHONE, body=f"x{i}", status="SENT")
        r = service.deliver(s, uid, "test", "one more")
    assert r["status"] == "FAILED" and r["error_code"] == wa.LOCAL_RATE_LIMIT and "10 minutes" in r["hint"]
    n = len(tw.sent)
    with session_scope() as s:
        assert service.deliver(s, uid, "test", "and another")["status"] == "FAILED"
    assert len(tw.sent) == n
    assert c.get("/api/whatsapp").json()["recent"][0]["hint"].startswith("Too many WhatsApp messages")


# ------------------------------------------------------------------ watches


def test_watch_api_validation_and_limits(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    assert c.post("/api/whatsapp/watches", json={"symbol": "RELIANCE", "interval_min": 3}, headers=csrf(c)).status_code == 422
    assert c.post("/api/whatsapp/watches", json={"symbol": "NOSUCHSYMBOLX", "interval_min": 5}, headers=csrf(c)).json()["code"] == "UNKNOWN_SYMBOL"
    r = c.post("/api/whatsapp/watches", json={"symbol": "reliance", "interval_min": 5, "min_move_pct": 0.5}, headers=csrf(c))
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["symbol"] == "RELIANCE" and w["market_hours_only"] is False and w["active"] is True and w["last_price"] is None
    p = c.patch(f"/api/whatsapp/watches/{w['id']}", json={"active": False}, headers=csrf(c))
    assert p.json()["active"] is False
    assert c.delete(f"/api/whatsapp/watches/{w['id']}", headers=csrf(c)).json() == {"deleted": True}
    with session_scope() as s:
        for i in range(wa.MAX_WATCHES):
            s.add(WhatsAppWatch(user_id=uid, instrument_id=f"NSE:EQ:FAKE{i}", symbol=f"FAKE{i}", interval_min=5))
    assert c.post("/api/whatsapp/watches", json={"symbol": "RELIANCE", "interval_min": 5}, headers=csrf(c)).json()["code"] == "WATCH_LIMIT"


def test_watch_schedule_interval_min_move_and_market_hours(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    set_tick("NSE:EQ:RELIANCE", 120760, prev=121245)
    c.post("/api/whatsapp/watches", json={"symbol": "RELIANCE", "interval_min": 2}, headers=csrf(c))
    c.post("/api/whatsapp/watches", json={"symbol": "INFY", "interval_min": 5, "min_move_pct": 1.0}, headers=csrf(c))
    set_tick("NSE:EQ:INFY", 186000, prev=185000)
    n = len(tw.sent)
    t0 = datetime.now(UTC)
    with session_scope() as s:
        assert service.watches_once(s, t0) == 1
    assert len(tw.sent) == n + 1  # one message per user with every due watch
    assert tw.sent[-1]["Body"] == "RELIANCE ₹1,207.60 (−0.4% today)\nINFY ₹1,860.00 (+0.5% today)"
    with session_scope() as s:
        assert service.watches_once(s, t0 + timedelta(minutes=1)) == 0  # not due yet
    set_tick("NSE:EQ:RELIANCE", 121000, prev=121245)
    set_tick("NSE:EQ:INFY", 186500, prev=185000)  # +0.27%: under INFY's 1% minimum move
    with session_scope() as s:
        assert service.watches_once(s, t0 + timedelta(minutes=2)) == 1
    assert tw.sent[-1]["Body"] == "RELIANCE ₹1,210.00 (−0.2% today, +0.2% since last update)"
    set_tick("NSE:EQ:INFY", 190000, prev=185000)  # +2.2% since the last sent price
    with session_scope() as s:
        assert service.watches_once(s, t0 + timedelta(minutes=5, seconds=1)) == 1
    assert "INFY ₹1,900.00 (+2.7% today, +2.2% since last update)" in tw.sent[-1]["Body"]
    # market_hours_only: nothing on a Saturday; sent on a weekday inside 09:15 to 15:30 IST.
    with session_scope() as s:
        s.execute(text("UPDATE whatsapp_watches SET market_hours_only = true, last_sent_at = NULL"))
    saturday = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)  # 11:30 IST, Saturday
    monday = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # 11:30 IST, Monday
    with session_scope() as s:
        assert service.watches_once(s, saturday) == 0
        assert service.watches_once(s, monday) == 1
    # Paused links get nothing.
    with session_scope() as s:
        s.execute(text("UPDATE whatsapp_watches SET market_hours_only = false, last_sent_at = NULL"))
        s.get(WhatsAppLink, uid).paused_until = datetime.now(UTC) + timedelta(hours=1)
    with session_scope() as s:
        assert service.watches_once(s) == 0


# ------------------------------------------------------------------ mandate cancel needs a fresh step-up


def _pending_mandate(user_id: str, account_id: str) -> str:
    from countersign.core.verifier import AccountState

    set_tick("NSE:EQ:INFY", 186000)
    acct = AccountState.load(FakeGateway(), account_id)
    text_ = "Buy 10 INFY if the price falls below 1800"
    with session_scope() as s:
        out = proposals.create_mandate(s, parse(text_).mandate, user_text=text_, account=acct, user_id=user_id, engine="deterministic", origin="test")
        return s.execute(select(StandingMandate.id).where(StandingMandate.proposal_id == out.proposal.id)).scalar_one()


def test_mandate_cancel_requires_fresh_step_up(api):
    main, _ = api
    c, me = signed_up(main)
    uid = me["user"]["id"]
    mid = _pending_mandate(uid, account_of(uid))
    r = c.post(f"/api/mandates/{mid}/cancel", headers=csrf(c))
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED" and r.json()["methods"] == []
    assert c.post("/api/auth/pin", json={"pin": PIN}, headers=csrf(c)).status_code == 200
    with session_scope() as s:
        s.execute(text("UPDATE sessions SET step_up_at = now() - interval '1 hour' WHERE user_id=:u"), {"u": uid})
    r = c.post(f"/api/mandates/{mid}/cancel", headers=csrf(c))
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED" and r.json()["methods"] == ["pin"]
    with session_scope() as s:
        assert s.get(StandingMandate, mid).state != "CANCELLED"
    assert c.post("/api/auth/step-up", json={"method": "pin", "pin": PIN}, headers=csrf(c)).status_code == 200
    r = c.post(f"/api/mandates/{mid}/cancel", headers=csrf(c))
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "CANCELLED"


def test_mandate_cancel_still_checks_ownership_first(api):
    main, _ = api
    a, me_a = signed_up(main)
    b, _ = signed_up(main)
    uid = me_a["user"]["id"]
    mid = _pending_mandate(uid, account_of(uid))
    assert b.post(f"/api/mandates/{mid}/cancel", headers=csrf(b)).status_code == 404


def test_alert_texts_are_short_and_plain():
    from countersign.core.models import OutcomeRow

    r = OutcomeRow(id="o1", kind="ALERT", title="Alert: ITC is down 3.10% today", detail="ITC is down 3.10% today (vs previous close). This alert fires at most once per holding per trading day (event e1). No order was placed.", severity="warning", account_id="X")
    assert service.outcome_text(r, None) == "Alert: ITC is down 3.10% today. No order was placed."
    m = StandingMandate(instrument_id="PORTFOLIO:HOLDINGS", kind="ALERT", predicate={"field": "DAY_CHANGE_BPS", "op": "LTE", "threshold": -300}, expires_at=datetime(2026, 10, 17, 10, 0, tzinfo=UTC))
    assert service.armed_text(m) == "Armed: if any holding is down 3.0% or more today, alert you (no order is placed). At most once per holding per day. Expires 17 Oct."
    m = StandingMandate(instrument_id="NSE:EQ:RELIANCE", kind="ORDER", predicate={"field": "LTP", "op": "LTE", "threshold": 115000}, template={"side": "BUY", "quantity": 10, "limit_price_paise": 115000}, expires_at=datetime(2026, 10, 17, 10, 0, tzinfo=UTC))
    assert service.armed_text(m) == "Armed: if RELIANCE falls to ₹1,150.00 or below, buy 10 (limit ₹1,150.00). Fires once. Expires 17 Oct."


def test_api_health_reports_no_twilio_credentials(api):
    main, _ = api
    h = new_client(main).get("/api/health").json()
    assert h["holds_twilio_credentials"] is False


def test_pause_and_resume_endpoints(api, tw):
    main, _ = api
    c, uid = linked(main, tw)
    assert c.post("/api/whatsapp/pause", json={"minutes": 0}, headers=csrf(c)).status_code == 422
    r = c.post("/api/whatsapp/pause", json={"minutes": 90}, headers=csrf(c))
    assert r.status_code == 200 and r.json()["paused_until"] and r.json()["link"]["paused_until"] == r.json()["paused_until"]
    assert c.get("/api/whatsapp").json()["link"]["paused_until"] == r.json()["paused_until"]
    assert c.post("/api/whatsapp/pause", json={"minutes": 90}).status_code == 403  # CSRF
    r = c.post("/api/whatsapp/resume", headers=csrf(c))
    assert r.status_code == 200 and r.json()["paused_until"] is None
    assert c.get("/api/whatsapp").json()["link"]["paused_until"] is None
    c.delete("/api/whatsapp/link", headers=csrf(c))
    assert c.post("/api/whatsapp/resume", headers=csrf(c)).status_code == 409


def test_state_is_available_when_the_notifier_is_down(api):
    main, _ = api
    wa._client[:] = [httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused"))), base_url="http://notifier")]
    try:
        c, _ = signed_up(main)
        r = c.get("/api/whatsapp")
        assert r.status_code == 200
        assert r.json()["notifier_up"] is False and r.json()["mode"] == "log_only" and r.json()["link"] is None
        assert r.json()["sandbox"]["number"] == "+1 415 523 8886"
    finally:
        wa._client.clear()


def test_sender_falls_back_to_the_sandbox_number_and_never_echoes_settings():
    odd = TwilioSettings(_env_file=None, account_sid="ACtest", auth_token=TOKEN, whatsapp_from="cow-piano")
    assert odd.configured and not odd.sender_valid and odd.sender == "whatsapp:+14155238886"
    ok = TwilioSettings(_env_file=None, account_sid="ACtest", auth_token=TOKEN, whatsapp_from="+14155238886")
    assert ok.sender_valid and ok.sender == "whatsapp:+14155238886"
    service.CONFIG[:] = [odd]
    try:
        h = TestClient(notifier_main.app).get("/health").json()
        assert h["sandbox_number"] == "+1 415 523 8886" and h["sender_valid"] is False and "piano" not in json.dumps(h)
    finally:
        service.CONFIG.clear()
