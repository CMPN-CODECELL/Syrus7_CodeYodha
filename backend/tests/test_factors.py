"""Approval methods cannot be taken over with the password or a session alone: adding a passkey, linking or replacing
the authenticator app and recovering the PIN all need a fresh proof with a method the trader already has. Also the
hardening around it: every active passkey can countersign, rate limits match exact paths, X-Forwarded-For is
ignored, security events are scoped to the caller, and the Attack Lab drill only releases its own halt."""

import pyotp
from sqlalchemy import select

from countersign.auth import countersign as cs
from countersign.core import state
from countersign.core.approval_crypto import b64url_decode
from countersign.core.db import session_scope
from countersign.core.models import Proposal, SecurityEvent, SessionRow, User
from countersign.llm.chat import ReasoningFilter, strip_reasoning
from tests.accounts_support import PASSWORD, SoftPasskey, csrf, link_totp, new_client, propose_for, signed_up
from tests.conftest import set_tick


def _victim(main):
    """A fully set up trader: PIN 7391, one passkey and a linked authenticator app."""
    c, me = signed_up(main)
    assert c.post("/api/auth/pin", json={"pin": "7391"}, headers=csrf(c)).status_code == 200
    pk = SoftPasskey()
    pk.register(me["user"]["id"])
    secret = link_totp(me["user"]["id"])
    return c, me, pk, secret


def _password_only(main, me):
    """A second client that knows only the password (or holds a stolen session)."""
    a = new_client(main)
    r = a.post("/api/auth/login", json={"email": me["email"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return a


def test_a_password_only_session_cannot_add_or_replace_approval_methods(api):
    main, _ = api
    _, me, _, secret = _victim(main)
    a = _password_only(main, me)
    for path, body in (("/api/auth/webauthn/register/start", {"device_label": "Attacker laptop"}), ("/api/auth/webauthn/register/finish", {}), ("/api/auth/totp/setup", None)):
        r = a.post(path, json=body, headers=csrf(a))
        assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED", (path, r.text)
        assert set(r.json()["methods"]) == {"webauthn", "pin", "totp"}
    r = a.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "8264"}, headers=csrf(a))
    assert r.status_code == 422 and r.json()["code"] == "TOTP_REQUIRED"
    wrong = a.post("/api/auth/step-up", json={"method": "pin", "pin": "1357"}, headers=csrf(a))
    assert wrong.status_code == 401 and wrong.json()["code"] == "PIN_INVALID"
    assert a.post("/api/auth/totp/setup", headers=csrf(a)).status_code == 403
    with session_scope() as s:
        u = s.get(User, me["user"]["id"])
        assert u.totp_confirmed and u.totp_pending_enc is None
    # The victim's own authenticator still countersigns.
    assert pyotp.TOTP(secret).now()


def test_a_fresh_step_up_unlocks_changes_and_a_replaced_authenticator_stays_active_until_confirmed(api):
    main, _ = api
    c, me, _, old_secret = _victim(main)
    other = new_client(main)
    assert other.post("/api/auth/login", json={"email": me["email"], "password": PASSWORD}).status_code == 200
    ok = other.post("/api/auth/step-up", json={"method": "pin", "pin": "7391"}, headers=csrf(other))
    assert ok.status_code == 200 and ok.json()["method"] == "pin" and ok.json()["expires_at"]
    assert other.post("/api/auth/webauthn/register/start", json={"device_label": "Second laptop"}, headers=csrf(other)).status_code == 200

    setup = other.post("/api/auth/totp/setup", headers=csrf(other))
    assert setup.status_code == 200
    new_secret = setup.json()["secret"]
    with session_scope() as s:
        u = s.get(User, me["user"]["id"])
        assert u.totp_confirmed and u.totp_pending_enc  # the linked app keeps working while the new one is pending
        from countersign.auth import factors

        factors.verify_totp(u, pyotp.TOTP(old_secret).now())
    assert other.post("/api/auth/totp/verify", json={"code": pyotp.TOTP(new_secret).now()}, headers=csrf(other)).status_code == 200
    with session_scope() as s:
        u = s.get(User, me["user"]["id"])
        assert u.totp_pending_enc is None and u.totp_confirmed
    kinds = {e["kind"] for e in c.get("/api/security/events").json()}
    assert {"AUTHENTICATOR_REPLACED"} <= kinds

    sid = other.get("/api/me").json()["session"]["id"]
    with session_scope() as s:
        row = s.get(SessionRow, sid)
        row.step_up_at = row.step_up_at.replace(year=2020)  # the window has passed
    assert other.post("/api/auth/totp/setup", headers=csrf(other)).json()["code"] == "STEP_UP_REQUIRED"


def test_onboarding_flows_from_pin_to_passkey_and_authenticator_without_extra_prompts(api):
    main, _ = api
    c, _ = signed_up(main)
    assert c.post("/api/auth/webauthn/register/start", json={}, headers=csrf(c)).status_code == 200  # no method yet
    assert c.post("/api/auth/pin", json={"pin": "4826"}, headers=csrf(c)).status_code == 200
    assert c.post("/api/auth/webauthn/register/start", json={}, headers=csrf(c)).status_code == 200
    assert c.post("/api/auth/totp/setup", headers=csrf(c)).status_code == 200


def test_pin_reset_with_a_passkey_needs_the_passkey_too_and_signs_out_other_sessions(api):
    main, _ = api
    c, me = signed_up(main)
    uid = me["user"]["id"]
    assert c.post("/api/auth/pin", json={"pin": "7391"}, headers=csrf(c)).status_code == 200
    pk = SoftPasskey()
    pk.register(uid)
    a = _password_only(main, me)
    r = a.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "8264"}, headers=csrf(a))
    assert r.status_code == 422 and r.json()["code"] == "PASSKEY_REQUIRED"
    pin_proof = a.post("/api/auth/step-up", json={"method": "pin", "pin": "7391"}, headers=csrf(a))
    assert pin_proof.status_code == 200
    assert a.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "8264"}, headers=csrf(a)).json()["code"] == "PASSKEY_REQUIRED"

    start = a.post("/api/auth/step-up/start", json={}, headers=csrf(a))
    assert start.status_code == 200
    opts = start.json()["options"]
    assert [x["id"] for x in opts["allowCredentials"]] == [pk.credential_id] and opts["userVerification"] == "required"
    proof = a.post("/api/auth/step-up", json={"method": "webauthn", "challenge_id": start.json()["challenge_id"], "credential": pk.assert_challenge(b64url_decode(opts["challenge"]))}, headers=csrf(a))
    assert proof.status_code == 200, proof.text
    replay = a.post("/api/auth/step-up", json={"method": "webauthn", "challenge_id": start.json()["challenge_id"], "credential": pk.assert_challenge(b64url_decode(opts["challenge"]))}, headers=csrf(a))
    assert replay.status_code == 410 and replay.json()["code"] == "CHALLENGE_EXPIRED"
    assert a.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "8264"}, headers=csrf(a)).status_code == 200
    assert c.get("/api/me").status_code == 401  # the other session was signed out by the recovery


def test_every_active_passkey_can_countersign_and_the_payload_names_them(api):
    main, _ = api
    c, me = signed_up(main)
    uid = me["user"]["id"]
    older, newer = SoftPasskey(), SoftPasskey()
    older.register(uid)
    newer.register(uid)
    stranger = SoftPasskey()
    stranger.register(signed_up(main)[1]["user"]["id"])
    pid = propose_for(uid, me["account"]["id"])
    start = c.post(f"/api/proposals/{pid}/approval/start", json={"method": "webauthn"}, headers=csrf(c))
    assert start.status_code == 200, start.text
    allowed = {x["id"] for x in start.json()["webauthn"]["allowCredentials"]}
    assert allowed == {older.credential_id, newer.credential_id}
    assert set(cs.signing_credentials(start.json()["payload"])) == allowed
    challenge = b64url_decode(start.json()["webauthn"]["challenge"])
    aid = start.json()["approval_id"]
    foreign = c.post(f"/api/proposals/{pid}/approval/complete", json={"approval_id": aid, "webauthn": stranger.assert_challenge(challenge)}, headers=csrf(c))
    assert foreign.status_code == 401 and foreign.json()["code"] == "SIGNATURE_INVALID"

    pid2 = propose_for(uid, me["account"]["id"], "buy 3 INFY at 1860")
    start = c.post(f"/api/proposals/{pid2}/approval/start", json={"method": "webauthn"}, headers=csrf(c)).json()
    set_tick("NSE:EQ:INFY", 186000)
    done = c.post(f"/api/proposals/{pid2}/approval/complete", json={"approval_id": start["approval_id"], "webauthn": older.assert_challenge(b64url_decode(start["webauthn"]["challenge"]))}, headers=csrf(c))
    assert done.status_code == 200, done.text
    with session_scope() as s:
        assert s.get(Proposal, pid2).status == "APPROVED"
        from countersign.core.models import Approval

        a = s.get(Approval, start["approval_id"])
        assert a.credential_id == older.credential_id and a.sign_count_before == 0


def test_rate_limits_match_exact_paths_say_how_long_to_wait_and_ignore_forwarded_for(api):
    main, _ = api
    c, _ = signed_up(main)
    for _ in range(7):
        c.post("/api/auth/pin", json={"pin": "1111"}, headers=csrf(c))
    for _ in range(5):
        r = c.post("/api/auth/pin/reset", json={"password": "wrong password", "new_pin": "7391"}, headers=csrf(c))
        assert r.status_code == 401, r.text  # PIN changes do not use up the recovery route's own budget
    limited = c.post("/api/auth/pin/reset", json={"password": "wrong password", "new_pin": "7391"}, headers=csrf(c))
    body = limited.json()
    assert limited.status_code == 429 and body["code"] == "RATE_LIMITED" and 1 <= body["retry_after_s"] <= 61
    assert "/" not in body["message"] and limited.headers["retry-after"] == str(body["retry_after_s"])

    main._RATE.clear()
    x = new_client(main)
    codes = [x.post("/api/auth/login", json={"email": f"x{i}@example.com", "password": "nope nope"}, headers={"X-Forwarded-For": f"10.66.0.{i}"}).status_code for i in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429  # a chosen X-Forwarded-For never opens a fresh bucket


def test_security_events_are_scoped_to_the_callers_account(api):
    main, _ = api
    a, me_a = signed_up(main)
    b, _ = signed_up(main)
    pid = propose_for(me_a["user"]["id"], me_a["account"]["id"])
    with session_scope() as s:
        state.security_event(s, "critical", "PAYLOAD_MISMATCH", "A's tampered approval", {"proposal_id": pid})
        state.security_event(s, "critical", "UNAUTHORIZED_BROKER_ORDER", "Lab rogue order", {"broker_order_id": "X1", "account_id": "LAB1", "source": "rogue"})
        state.security_event(s, "critical", "PAYLOAD_MISMATCH", "Lab drill refusal", {"account_id": "LAB1"})
        state.security_event(s, "critical", "AUDIT_CHAIN_BROKEN", "System-wide")
    mine_a = {e["message"]: e for e in a.get("/api/security/events").json()}
    mine_b = {e["message"] for e in b.get("/api/security/events").json()}
    assert set(mine_a) == {"A's tampered approval", "System-wide"} and mine_b == {"System-wide"}
    assert mine_a["A's tampered approval"]["scope"] == "account" and mine_a["System-wide"]["scope"] == "system"
    every = {e["message"]: e for e in b.get("/api/security/events?scope=all").json()}
    assert set(every) == {"System-wide", "Lab rogue order", "Lab drill refusal"}
    assert every["Lab drill refusal"]["scope"] == "drill" and every["Lab drill refusal"]["resolved"] is True
    assert every["Lab rogue order"]["resolved"] is False  # stays open until the drill marks it contained
    with session_scope() as s:
        assert s.execute(select(SecurityEvent.account_id).where(SecurityEvent.message == "A's tampered approval")).scalar_one() == me_a["account"]["id"]


def test_a_drill_releases_only_the_halt_it_caused():
    with session_scope() as s:
        seen = state.set_kill_switch(s, True, "Invariant monitor: Broker order LAB-77 has no countersigned approval.", "monitor")
    with session_scope() as s:
        state.set_kill_switch(s, True, "Engaged by trader from the Security Center", "trader")  # someone else, later
    with session_scope() as s:
        assert state.release_kill_switch_if(s, seen, "drill finished", "attack_lab") is False
        assert state.kill_switch(s)["engaged"] is True
        now = state.kill_switch(s)
    with session_scope() as s:
        assert state.release_kill_switch_if(s, now, "released", "attack_lab") is True
        assert state.kill_switch(s)["engaged"] is False


def test_model_reasoning_never_reaches_the_chat():
    leaked = '<think> The user: "Buy 1 TCS at 4007". Need product type. We ask for clarification.Which product type would you like?'
    f = ReasoningFilter()
    streamed = "".join(f.feed(leaked[i : i + 5]) for i in range(0, len(leaked), 5)) + f.finish()
    assert streamed == "Which product type would you like?"
    assert strip_reasoning("<think>\nplan\n</think>\n\nYour draft is ready.") == "Your draft is ready."
    assert strip_reasoning("Your P&L today is +₹1,250.00.") == "Your P&L today is +₹1,250.00."
