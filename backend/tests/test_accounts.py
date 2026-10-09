"""Real accounts: sign-up provisions a paper account and a fresh session, password sign-in with lockout, passkey
sign-in with discoverable credentials, the PIN chosen by the trader (weak PINs refused) and PIN recovery."""

import re
import time
from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy import select

from countersign.auth import factors
from countersign.auth import virtual_authenticator as vauth
from countersign.core.approval_crypto import b64url_decode
from countersign.core.db import session_scope
from countersign.core.models import Account, DeviceCredential, SessionRow, User
from tests.accounts_support import PASSWORD, SoftPasskey, csrf, email, expire_login_locks, link_totp, new_client, signed_up, signup


def signed_up_as(main, addr: str):
    c = new_client(main)
    r = signup(c, addr)
    assert r.status_code == 201, r.text
    return c, r.json()


def test_signup_creates_user_paper_account_and_session(api):
    main, admin = api
    c = new_client(main)
    addr = email("Asha")
    r = signup(c, addr, name="  Asha Rao  ")
    assert r.status_code == 201, r.text
    me = r.json()
    assert me["email"] == addr.lower() and me["user"]["display_name"] == "Asha Rao"
    acct = me["account"]["id"]
    assert re.fullmatch(r"PT[A-Z2-7]{6}", acct) and me["account_id"] == acct and me["account"]["label"]
    assert admin.calls == [("create", acct)]
    assert me["onboarding"] == {"pin_set": False, "passkeys": 0, "totp": False, "complete": False}
    assert me["has_pin"] is False and me["has_totp"] is False and me["devices"] == [] and me["demo_mode"] is True
    assert c.cookies.get("cs_session") and c.cookies.get("cs_csrf")
    with session_scope() as s:
        u = s.execute(select(User).where(User.email == addr.lower())).scalar_one()
        assert u.password_hash and u.password_hash.startswith("$argon2id$") and u.pin_hash is None
        assert s.get(Account, acct).user_id == u.id
        assert s.get(SessionRow, me["session"]["id"]).user_id == u.id
    again = c.get("/api/me").json()
    assert again["account"]["id"] == acct and again["session"]["id"] == me["session"]["id"]


def test_signup_rejects_duplicate_email_case_insensitively(api):
    main, _ = api
    addr = email()
    assert signup(new_client(main), addr).status_code == 201
    r = signup(new_client(main), addr.upper())
    assert r.status_code == 409 and r.json()["code"] == "EMAIL_TAKEN"


def test_signup_validates_each_field(api):
    main, admin = api
    c = new_client(main)
    addr = email()
    cases = [
        ({"display_name": "", "email": addr, "password": PASSWORD}, "display_name"),
        ({"display_name": "A", "email": "not-an-email", "password": PASSWORD}, "email"),
        ({"display_name": "A", "email": addr, "password": "short"}, "password"),
        ({"display_name": "A", "email": addr, "password": addr.upper()}, "password"),
    ]
    for body, field in cases:
        r = c.post("/api/auth/signup", json=body)
        assert r.status_code == 422 and r.json()["code"] == "INVALID_INPUT" and r.json()["field"] == field, (body, r.text)
    assert admin.calls == []


def test_login_success_mints_a_new_session_and_failures_look_identical(api):
    main, _ = api
    addr = email()
    c = new_client(main)
    r = signup(c, addr)
    assert r.status_code == 201
    old_sid = r.json()["session"]["id"]
    ok = c.post("/api/auth/login", json={"email": addr.upper(), "password": PASSWORD})
    assert ok.status_code == 200, ok.text
    assert ok.json()["session"]["id"] != old_sid and ok.json()["email"] == addr
    assert c.get("/api/me").json()["session"]["id"] == ok.json()["session"]["id"]
    with session_scope() as s:
        assert s.get(SessionRow, old_sid).revoked_at is not None  # the presented session is replaced, never reused
    wrong = new_client(main).post("/api/auth/login", json={"email": addr, "password": "wrong password!"})
    unknown = new_client(main).post("/api/auth/login", json={"email": email("ghost"), "password": "wrong password!"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"code": "INVALID_CREDENTIALS", "message": "Email or password is incorrect."}


def test_login_locks_after_five_failures_with_backoff(api):
    main, _ = api
    addr = email()
    assert signup(new_client(main), addr).status_code == 201
    c = new_client(main)
    for _ in range(4):
        assert c.post("/api/auth/login", json={"email": addr, "password": "nope nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": addr, "password": "nope nope"})
    assert r.status_code == 429 and r.json()["code"] == "LOGIN_LOCKED" and 25 <= r.json()["retry_after_s"] <= 30
    assert r.headers.get("retry-after")
    blocked = c.post("/api/auth/login", json={"email": addr, "password": PASSWORD})
    assert blocked.status_code == 429 and blocked.json()["code"] == "LOGIN_LOCKED"
    expire_login_locks(main, addr)
    r6 = c.post("/api/auth/login", json={"email": addr, "password": "nope nope"})
    assert r6.status_code == 429 and 55 <= r6.json()["retry_after_s"] <= 60  # 6th failure doubles the wait
    expire_login_locks(main, addr)
    ok = c.post("/api/auth/login", json={"email": addr, "password": PASSWORD})
    assert ok.status_code == 200
    with session_scope() as s:
        u = s.execute(select(User).where(User.email == addr)).scalar_one()
        assert u.login_failed_count == 0 and u.login_locked_until is None
    assert not [k for k in main._FAILS if k[0] == addr]


def test_a_stranger_cannot_lock_the_owner_out(api):
    """The lock is per (email, client), forgets failures after a quiet window, and a passkey sign-in lifts it."""
    main, _ = api
    addr = email()
    _, me = signed_up_as(main, addr)
    stranger = new_client(main, ip="10.9.8.7")
    for _ in range(5):
        stranger.post("/api/auth/login", json={"email": addr, "password": "nope nope"})
    assert stranger.post("/api/auth/login", json={"email": addr, "password": PASSWORD}).json()["code"] == "LOGIN_LOCKED"
    assert new_client(main).post("/api/auth/login", json={"email": addr, "password": PASSWORD}).status_code == 200

    key = (addr, "10.9.8.7")
    main._FAILS[key] = (9, 0.0, time.time() - main.LOGIN_WINDOW_S - 5)  # last failure long ago: the count restarts
    assert stranger.post("/api/auth/login", json={"email": addr, "password": "nope nope"}).status_code == 401
    assert main._FAILS[key][0] == 1

    main._RATE.clear()  # sign-in is also limited to 10 requests a minute per client; start a fresh window
    for _ in range(5):
        stranger.post("/api/auth/login", json={"email": addr, "password": "nope nope"})
    assert main._client_locked_for(key) > 0
    pk = SoftPasskey()
    pk.register(me["user"]["id"])
    assert _passkey_login(new_client(main), pk, me["user"]["id"])[2].status_code == 200
    assert main._client_locked_for(key) == 0  # the owner's passkey sign-in lifts every password lock


def test_unknown_email_gets_the_same_lockout(api):
    main, _ = api
    c = new_client(main)
    ghost = email("ghost")
    codes = [c.post("/api/auth/login", json={"email": ghost, "password": "nope nope"}).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 401, 429]


def test_signin_endpoints_reject_foreign_origins(api):
    main, _ = api
    c = new_client(main)
    evil = {"Origin": "https://evil.example"}
    for path, body in (("/api/auth/login", {"email": email(), "password": PASSWORD}), ("/api/auth/signup", {"display_name": "X", "email": email(), "password": PASSWORD}), ("/api/auth/passkey/login/start", {})):
        r = c.post(path, json=body, headers=evil)
        assert r.status_code == 403 and r.json()["code"] == "ORIGIN_REJECTED", path
    ok = c.post("/api/auth/passkey/login/start", json={}, headers={"Origin": "http://localhost:3000"})
    assert ok.status_code == 200


def test_login_is_rate_limited_per_ip(api):
    main, _ = api
    c = new_client(main)
    codes = [c.post("/api/auth/login", json={"email": f"x{i}@example.com", "password": "nope nope"}).status_code for i in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def _passkey_login(c, pk: SoftPasskey, user_id: str):
    start = c.post("/api/auth/passkey/login/start", json={})
    assert start.status_code == 200, start.text
    opts = start.json()["options"]
    assert opts["allowCredentials"] == [] and opts["userVerification"] == "required" and opts["rpId"] == "localhost"
    cred = pk.login_assertion(b64url_decode(opts["challenge"]), user_id)
    return start.json()["challenge_id"], cred, c.post("/api/auth/passkey/login/finish", json={"challenge_id": start.json()["challenge_id"], "credential": cred})


def test_passkey_login_happy_path_updates_sign_counter(api):
    main, _ = api
    _, me = signed_up(main)
    uid = me["user"]["id"]
    pk = SoftPasskey()
    dev = pk.register(uid)
    c = new_client(main)
    cid, cred, r = _passkey_login(c, pk, uid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["user"]["id"] == uid and out["session"]["id"] != me["session"]["id"] and out["onboarding"]["passkeys"] == 1
    assert c.get("/api/me").status_code == 200
    with session_scope() as s:
        d = s.get(DeviceCredential, dev)
        assert d.sign_count == 1 and d.last_used_at is not None
    replay = c.post("/api/auth/passkey/login/finish", json={"challenge_id": cid, "credential": cred})
    assert replay.status_code == 410 and replay.json()["code"] == "CHALLENGE_EXPIRED"


def test_passkey_login_rejects_revoked_virtual_lab_and_forged(api):
    main, _ = api
    _, me = signed_up(main)
    uid = me["user"]["id"]
    c = new_client(main)

    revoked = SoftPasskey()
    rid = revoked.register(uid)
    with session_scope() as s:
        s.get(DeviceCredential, rid).revoked_at = datetime.now(UTC)
    assert _passkey_login(c, revoked, uid)[2].json()["code"] == "PASSKEY_NOT_RECOGNIZED"

    virtual = SoftPasskey()
    virtual.register(uid, is_virtual=True)
    r = _passkey_login(c, virtual, uid)[2]
    assert r.status_code == 401 and r.json()["code"] == "PASSKEY_NOT_RECOGNIZED"

    with session_scope() as s:
        lab_id = s.execute(select(User.id).where(User.username == "attack-lab")).scalar_one()
    start = c.post("/api/auth/passkey/login/start", json={}).json()
    lab_cred = vauth.get().assert_challenge(b64url_decode(start["options"]["challenge"]))
    r = c.post("/api/auth/passkey/login/finish", json={"challenge_id": start["challenge_id"], "credential": lab_cred})
    assert r.status_code == 401 and r.json()["code"] == "PASSKEY_NOT_RECOGNIZED" and lab_id

    unknown = SoftPasskey()
    assert _passkey_login(c, unknown, uid)[2].json()["code"] == "PASSKEY_NOT_RECOGNIZED"

    good = SoftPasskey()
    good.register(uid)
    start = c.post("/api/auth/passkey/login/start", json={}).json()
    forged = good.login_assertion(b"some other challenge the server never issued", uid)
    r = c.post("/api/auth/passkey/login/finish", json={"challenge_id": start["challenge_id"], "credential": forged})
    assert r.status_code == 401 and r.json()["code"] == "SIGNATURE_INVALID"
    assert "cs_session" not in c.cookies


def test_trader_chooses_pin_and_weak_pins_are_refused(api):
    main, _ = api
    c, _ = signed_up(main)
    for weak in ("1111", "1234", "2345", "0123", "4321", "9876"):
        r = c.post("/api/auth/pin", json={"pin": weak}, headers=csrf(c))
        assert r.status_code == 422 and r.json()["code"] == "WEAK_PIN", weak
    assert c.post("/api/auth/pin", json={"pin": "12a4"}, headers=csrf(c)).json()["code"] == "PIN_FORMAT"
    for not_ascii in ("\u00b2\u00b2\u00b2\u00b2", "\u0661\u0663\u0665\u0667", "\uff11\uff13\uff15\uff17"):  # superscript, Arabic-Indic, full-width
        r = c.post("/api/auth/pin", json={"pin": not_ascii}, headers=csrf(c))
        assert r.status_code == 422 and r.json()["code"] == "PIN_FORMAT", (not_ascii, r.text)
    assert c.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "\u00b3\u00b3\u00b3\u00b3"}, headers=csrf(c)).json()["code"] == "PIN_FORMAT"
    main._RATE.clear()  # PIN changes are limited to 10 a minute; start a fresh window
    ok = c.post("/api/auth/pin", json={"pin": "4826"}, headers=csrf(c))
    assert ok.status_code == 200
    me = c.get("/api/me").json()
    assert me["has_pin"] is True and me["onboarding"]["pin_set"] is True and me["onboarding"]["complete"] is True


def test_pin_reset_needs_the_account_password_and_authenticator(api):
    main, _ = api
    c, me = signed_up(main)
    assert c.post("/api/auth/pin", json={"pin": "4826"}, headers=csrf(c)).status_code == 200
    with session_scope() as s:
        u = s.get(User, me["user"]["id"])
        u.pin_failed_count, u.pin_locked_until = 5, datetime.now(UTC) + timedelta(minutes=15)
    wrong = c.post("/api/auth/pin/reset", json={"password": "not my password", "new_pin": "7391"}, headers=csrf(c))
    assert wrong.status_code == 401 and wrong.json()["code"] == "INVALID_CREDENTIALS"
    weak = c.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "5555"}, headers=csrf(c))
    assert weak.status_code == 422 and weak.json()["code"] == "WEAK_PIN"
    ok = c.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "7391"}, headers=csrf(c))
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    with session_scope() as s:
        u = s.get(User, me["user"]["id"])
        assert u.pin_locked_until is None and u.pin_failed_count == 0
        factors.verify_pin(s, u, "7391")
    secret = link_totp(me["user"]["id"])
    main._RATE.clear()  # PIN reset is limited to 5 attempts a minute; start a fresh window
    need = c.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "6157"}, headers=csrf(c))
    assert need.status_code == 422 and need.json()["code"] == "TOTP_REQUIRED"
    bad = c.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "6157", "totp": "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"}, headers=csrf(c))
    assert bad.status_code == 422 and bad.json()["code"] == "TOTP_INVALID"
    good = c.post("/api/auth/pin/reset", json={"password": PASSWORD, "new_pin": "6157", "totp": pyotp.TOTP(secret).now()}, headers=csrf(c))
    assert good.status_code == 200


def test_demo_login_is_for_scripts_and_never_opens_a_real_account(api):
    main, _ = api
    _, me = signed_up(main)
    c = new_client(main)
    r = c.post("/api/auth/demo-login", json={"username": me["user"]["username"]})
    assert r.status_code == 404
    assert c.post("/api/auth/demo-login", json={"username": "attack-lab"}).status_code == 403
    demo = c.post("/api/auth/demo-login", json={})
    assert demo.status_code == 200 and demo.json()["account"]["id"] == "DEMO1" and demo.json()["email"] is None
    # Only scripts on this machine calling the API directly: never through the web proxy, a browser or the network.
    for headers in ({"X-Forwarded-Host": "192.168.41.33:3000"}, {"X-Forwarded-For": "127.0.0.1"}, {"Origin": "https://evil.example"}):
        r = new_client(main).post("/api/auth/demo-login", json={}, headers=headers)
        assert r.status_code == 403 and "cs_session" not in r.cookies, headers
    assert new_client(main, ip="192.168.41.50").post("/api/auth/demo-login", json={}).status_code == 403


def test_kill_switch_release_requires_the_callers_pin(api):
    main, _ = api
    c, _ = signed_up(main)
    assert c.post("/api/security/kill-switch", json={"engaged": True}, headers=csrf(c)).status_code == 200
    r = c.post("/api/security/kill-switch", json={"engaged": False}, headers=csrf(c))
    assert r.status_code == 422 and r.json()["code"] == "PIN_NOT_SET"
    assert c.post("/api/auth/pin", json={"pin": "4826"}, headers=csrf(c)).status_code == 200
    ok = c.post("/api/security/kill-switch", json={"engaged": False, "pin": "4826"}, headers=csrf(c))
    assert ok.status_code == 200 and ok.json()["kill_switch"]["engaged"] is False


def test_passkey_registration_is_discoverable_and_accepts_a_long_device_label(api):
    main, _ = api
    c, me = signed_up(main)
    r = c.post("/api/auth/webauthn/register/start", json={"device_label": "Windows Hello on the team demo laptop (Chrome)"}, headers=csrf(c))
    assert r.status_code == 200, r.text
    opts = r.json()
    assert opts["authenticatorSelection"]["residentKey"] == "required" and opts["user"]["name"] == me["email"]
