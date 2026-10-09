"""Self-service 021 linking: the browser encrypts {ucc, password, nonce, user_id} to the gateway's RSA key, the API relays
only the ciphertext (with the nonce it issued and the caller's user id) and the gateway verifies the password with one
021 login before storing it sealed. Runs the real API and the real gateway app in-process; 021 itself is a fake
(httpx MockTransport). No test calls the live sandbox."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from sqlalchemy import text

from countersign.broker021 import app as gapp
from countersign.broker021 import linking, store
from countersign.broker021.client import Client021
from countersign.broker021.gateway import Gateway
from countersign.broker021 import vault as vault_mod
from countersign.broker021.client import login_failure
from countersign.broker021.settings import GatewaySettings
from countersign.broker021.vault import OAEP, Vault
from countersign.core.broker_client import _kinds
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import Account, BrokerLink, User, WebauthnChallenge
from tests.accounts_support import PASSWORD, csrf, email, new_client, signup
from tests.test_021_gateway import FakeLookup

ENV = "ENVX0001"  # the account configured in .env (operator-linked)
MINE = "SELF1234"  # a trader's own 021 account
OTHER = "SELF5678"
OLD = "OLDX0001"  # a 021 row left as 'operator' by an earlier .env UCC
WRONG = [f"WRNG000{i}" for i in range(10)]  # UCCs unknown to the fake 021 (every login to them fails)
PW_ENV = "env-pass-Zq81"
PW_MINE = "my-021-pass-Kv72"
PW_OTHER = "other-pass-Hh39"
PIN = "4826"


class Fake021Multi:
    """A scripted 021 with several accounts: logins check (UCC, password); every read records which account's token
    it carried, so tests can prove X-Account reaches the right session."""

    def __init__(self):
        self.passwords = {ENV: PW_ENV, MINE: PW_MINE, OTHER: PW_OTHER}
        self.logins: list[tuple[str, bool]] = []
        self.reads: list[tuple[str, str]] = []
        self.login_fault: str | None = None  # "503" | "timeout"
        self.login_status: int | None = None  # answer every login with this status and a raw 021 error text
        self.tokens: dict[str, str] = {}
        self.n = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/v1", 1)[1]
        if path == "/auth/token":
            body = json.loads(req.content or b"{}")
            if self.login_fault == "503":
                return httpx.Response(503, json={"data": None, "success": False, "error": "Service temporarily unavailable"})
            if self.login_fault == "timeout":
                raise httpx.ReadTimeout("read timed out", request=req)
            if self.login_status:
                self.logins.append((body.get("username"), False))
                return httpx.Response(self.login_status, json={"data": None, "success": False, "error": "raw-021-text internal-detail-7731"})
            ok = self.passwords.get(body.get("username")) == body.get("password")
            self.logins.append((body.get("username"), ok))
            if not ok:
                return httpx.Response(401, json={"data": None, "success": False, "error": "Invalid credentials"})
            self.n += 1
            tok = f"tok-{body['username']}-{self.n}"
            self.tokens[tok] = body["username"]
            return httpx.Response(200, json={"data": {"accessToken": tok, "expiresAt": "2099-01-01T05:00:00+05:30", "expiresIn": 3600}, "success": True, "error": None})
        who = self.tokens.get(req.headers.get("authorization", "")[7:])
        if who is None:
            return httpx.Response(401, json={"data": None, "success": False, "error": "Invalid or expired access token."})
        self.reads.append((who, path))
        if path in ("/orders", "/trades", "/portfolio/positions", "/portfolio/holdings"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": "no route"})


class _NoLifespan:
    """The gateway app as the API's HTTP client, without running the gateway's startup (threads, websockets)."""

    def __init__(self, c: TestClient):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *exc):
        return False


def _cfg(ucc: str = ENV, password: str = PW_ENV) -> GatewaySettings:
    return GatewaySettings(ucc=ucc, password=password, reconcile_window_s=0.3, read_retries=0, market_ws=False, orders_ws=False)


@pytest.fixture
def stack(api, monkeypatch, tmp_path):
    """The real API and the real gateway app wired together in-process; 021 is Fake021Multi."""
    main, admin = api
    fake = Fake021Multi()
    transport = httpx.MockTransport(fake.handler)
    cfg = settings()
    for name, val in (("broker_read_key", "k-read"), ("broker_admin_key", "k-admin"), ("broker_audit_key", "k-audit"), ("gateway021_enabled", True), ("owner_emails_021", "")):
        monkeypatch.setattr(cfg, name, val)
    store.init_db()
    vault = Vault(tmp_path / "secrets")
    env_gw = Gateway(_cfg(), client=Client021(_cfg(), transport=transport, sleep=lambda s: None), lookup=FakeLookup(), publish=lambda o: None)
    monkeypatch.setattr(gapp, "vault", lambda: vault)
    monkeypatch.setattr(gapp, "_gw", env_gw)
    monkeypatch.setattr(gapp, "_linked", {})
    monkeypatch.setattr(gapp, "_used_nonces", {})
    monkeypatch.setattr(gapp, "_pending", {})
    monkeypatch.setattr(gapp, "_transport", transport)
    monkeypatch.setattr(gapp, "_start_poller", lambda g: None)
    monkeypatch.setattr(gapp, "_ensure_market", lambda: None)
    gclient = TestClient(gapp.app, client=("127.0.0.1", 50000))
    monkeypatch.setattr(linking, "gateway_http", lambda: _NoLifespan(gclient))
    monkeypatch.setattr(linking, "gateway_account_id", lambda refresh=False: ENV)
    main._LINK_KEY.update(value=None, at=0.0)
    main._STATUS_KEYS.clear()
    for k in [k for k in main._RATE if k.startswith("link021:")]:
        main._RATE.pop(k, None)
    _wipe()
    yield main, fake, gclient, vault
    _wipe()


def _wipe() -> None:
    with store.session() as s:
        for model in (store.Credential, store.PendingCredential, store.LinkFailure):
            s.query(model).delete(synchronize_session=False)
        s.commit()
    with session_scope() as s:
        for a in (ENV, MINE, OTHER, OLD, *WRONG):
            s.execute(text("DELETE FROM broker_links WHERE account_id=:a"), {"a": a})
            s.execute(text("DELETE FROM account_members WHERE account_id=:a"), {"a": a})
            s.execute(text("UPDATE users SET active_account_id=NULL WHERE active_account_id=:a"), {"a": a})
            s.execute(text("DELETE FROM accounts WHERE id=:a"), {"a": a})
    for a in (ENV, MINE, OTHER, OLD):
        _kinds.pop(a, None)


def trader(main, ip: str = "127.0.0.1", addr: str | None = None) -> tuple[TestClient, str]:
    """A signed-up trader whose first PIN counts as a fresh step-up (as in onboarding)."""
    for k in [k for k in main._RATE if k.endswith("/api/auth/signup")]:
        main._RATE.pop(k, None)  # sign-up allows 5 a minute per IP; some tests sign up more traders than that
    c = new_client(main, ip)
    r = signup(c, addr)
    assert r.status_code == 201, r.text
    assert c.post("/api/auth/pin", json={"pin": PIN}, headers=csrf(c)).status_code == 200
    return c, r.json()["user"]["id"]


def encrypt(c: TestClient, ucc: str, password: str, *, nonce: str | None = None, user_id: str | None = None) -> str:
    """What the browser does: RSA-OAEP SHA-256 over the JSON, to the gateway's public key from the API."""
    k = c.get("/api/broker/021/link-key")
    assert k.status_code == 200, k.text
    key = k.json()
    pub = serialization.load_der_public_key(base64.b64decode(key["public_key"]))
    pt = json.dumps({"ucc": ucc, "password": password, "nonce": nonce or key["nonce"], "user_id": user_id or key["user_id"]}).encode()
    assert len(pt) <= key["max_plaintext_bytes"]
    return base64.b64encode(pub.encrypt(pt, OAEP)).decode()


def link(c: TestClient, ucc: str, password: str, **kw) -> httpx.Response:
    return c.post("/api/broker/021/link", json={"ciphertext": encrypt(c, ucc, password, **kw)}, headers=csrf(c))


def unlink(c: TestClient, account_id: str) -> httpx.Response:
    return c.request("DELETE", "/api/broker/021/link", json={"account_id": account_id}, headers=csrf(c))


def creds() -> list[tuple[str, str]]:
    with store.session() as s:
        return [(r.ucc, r.linked_user_id) for r in s.query(store.Credential).all()]


def _all_table_text() -> str:
    """Every row of every table in the test database (API tables and the gateway's), as text."""
    out = []
    with session_scope() as s:
        tables = s.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")).scalars().all()
        for t in tables:
            out += [str(r) for r in s.execute(text(f'SELECT t::text FROM "{t}" t')).scalars()]
    return "\n".join(out)


# ------------------------------------------------------------------ crypto


def test_vault_round_trips_link_ciphertext_and_sealed_passwords(tmp_path):
    v = Vault(tmp_path)
    pub = serialization.load_der_public_key(base64.b64decode(v.public_key_spki_b64()))
    ct = base64.b64encode(pub.encrypt(json.dumps({"ucc": MINE, "password": PW_MINE, "nonce": "n", "user_id": "u"}).encode(), OAEP)).decode()
    assert v.open_link(ct)["password"] == PW_MINE
    sealed = v.seal(PW_MINE, MINE, "u")
    assert PW_MINE not in sealed and v.unseal(sealed, MINE, "u") == PW_MINE
    # Associated data binds the sealed password to its UCC and user: it cannot be moved to another row.
    from countersign.broker021.vault import VaultError

    with pytest.raises(VaultError):
        v.unseal(sealed, OTHER, "u")
    with pytest.raises(VaultError):
        v.open_link(base64.b64encode(b"x" * 256).decode())
    # The key files are created once and reused.
    assert Vault(tmp_path).public_key_spki_b64() == v.public_key_spki_b64()


# ------------------------------------------------------------------ linking


def test_link_verifies_once_stores_sealed_and_activates_the_account(stack):
    main, fake, _, vault = stack
    c, uid = trader(main)
    r = link(c, MINE.lower(), PW_MINE)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["masked_ucc"] == "SELF***4" and out["active_account_id"] == MINE
    assert out["account"]["linked_by"] == "self" and out["account"]["masked_id"] == "SELF***4"
    assert fake.logins == [(MINE, True)]  # verified with exactly one 021 login, and that token is kept
    assert creds() == [(MINE, uid)]
    assert c.get("/api/me").json()["account"]["kind"] == "021"
    rows = {a["kind"]: a for a in c.get("/api/broker/accounts").json()}
    assert rows["021"]["masked_id"] == "SELF***4" and rows["021"]["active"]
    # The password is stored sealed: it appears in no table of the database, in any form the tests can see.
    dump = _all_table_text()
    assert PW_MINE not in dump and base64.b64encode(PW_MINE.encode()).decode() not in dump
    with store.session() as s:
        row = s.query(store.Credential).one()
        assert vault.unseal(row.password_enc, MINE, uid) == PW_MINE
    with session_scope() as s:
        audit = [r for r in s.execute(text("SELECT data::text FROM audit_events WHERE event_type = 'ACCOUNT_LINKED_021' AND data->>'model' = 'self-service'")).scalars()]
    assert any("SELF***4" in a for a in audit) and not any(x in a for a in audit for x in (MINE, OTHER, ENV))


def test_wrong_password_or_unavailable_021_stores_nothing(stack):
    main, fake, _, _ = stack
    c, _ = trader(main)
    r = link(c, MINE, "not-the-password")
    assert r.status_code == 401 and r.json()["code"] == "LINK_WRONG_CREDENTIALS"
    fake.login_fault = "503"
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 503 and r.json()["code"] == "LINK_021_UNAVAILABLE" and "try again" in r.json()["message"]
    fake.login_fault = "timeout"
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 503 and r.json()["code"] == "LINK_021_UNAVAILABLE"
    assert creds() == [] and gapp._linked == {}
    assert [a["kind"] for a in c.get("/api/broker/accounts").json()] == ["paper"]


def test_replayed_stale_or_foreign_nonces_are_refused(stack):
    main, fake, gclient, _ = stack
    c, uid = trader(main)
    ct = encrypt(c, MINE, PW_MINE)
    assert c.post("/api/broker/021/link", json={"ciphertext": ct}, headers=csrf(c)).status_code == 200
    # Replaying the same ciphertext: its nonce was consumed by the API.
    r = c.post("/api/broker/021/link", json={"ciphertext": ct}, headers=csrf(c))
    assert r.status_code == 400 and r.json()["code"] == "LINK_NONCE_INVALID"
    # Even straight at the gateway with the right nonce, an opened nonce is never accepted twice.
    with session_scope() as s:
        used = s.execute(text("SELECT challenge FROM webauthn_challenges WHERE user_id=:u AND purpose='LINK_021' ORDER BY created_at DESC LIMIT 1"), {"u": uid}).scalar()
    from countersign.core.approval_crypto import b64url

    r = gclient.post("/admin/link", json={"ciphertext": ct, "nonce": b64url(used), "user_id": uid}, headers={"Authorization": "Bearer k-admin"})
    assert r.status_code == 400 and r.json()["code"] == "LINK_REPLAYED"
    # A stale nonce (older than 5 minutes) is refused.
    ct = encrypt(c, OTHER, PW_OTHER)
    with session_scope() as s:
        s.execute(text("UPDATE webauthn_challenges SET expires_at = now() - interval '1 minute' WHERE user_id=:u AND purpose='LINK_021' AND used_at IS NULL"), {"u": uid})
    r = c.post("/api/broker/021/link", json={"ciphertext": ct}, headers=csrf(c))
    assert r.status_code == 400 and r.json()["code"] == "LINK_NONCE_INVALID"
    # Ciphertext made with another user's id (or another nonce) does not match what the API expects for this user.
    r = link(c, OTHER, PW_OTHER, user_id="someone-else")
    assert r.status_code == 400 and r.json()["code"] == "LINK_NONCE_MISMATCH"
    r = link(c, OTHER, PW_OTHER, nonce="an-old-nonce")
    assert r.status_code == 400 and r.json()["code"] == "LINK_NONCE_MISMATCH"
    assert creds() == [(MINE, uid)] and [u for u, _ in fake.logins] == [MINE]


def test_link_and_unlink_need_a_fresh_step_up(stack, monkeypatch):
    main, fake, _, _ = stack
    c, uid = trader(main)
    with session_scope() as s:
        s.execute(text("UPDATE sessions SET step_up_at = now() - interval '1 hour' WHERE user_id=:u"), {"u": uid})
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED" and fake.logins == []
    assert c.post("/api/auth/step-up", json={"method": "pin", "pin": PIN}, headers=csrf(c)).status_code == 200
    assert link(c, MINE, PW_MINE).status_code == 200
    with session_scope() as s:
        s.execute(text("UPDATE sessions SET step_up_at = now() - interval '1 hour' WHERE user_id=:u"), {"u": uid})
    r = unlink(c, MINE)
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED" and creds() == [(MINE, uid)]
    # A trader without any approval method cannot connect a broker account at all.
    c2 = new_client(main)
    assert signup(c2).status_code == 201
    r = link(c2, OTHER, PW_OTHER)
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED" and r.json()["methods"] == []


def test_link_attempts_are_rate_limited_per_user(stack):
    main, fake, _, _ = stack
    c, uid = trader(main)
    codes = [link(c, WRONG[i], "wrong-password").status_code for i in range(5)]
    assert codes == [401] * 5
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and r.json()["retry_after_s"] > 0
    assert len(fake.logins) == 5 and creds() == []
    assert "RATE_LIMITED" in _refusal_reasons(uid)


def test_link_attempts_have_a_per_user_daily_cap(stack):
    main, fake, _, _ = stack
    c, uid = trader(main)
    # Ten attempts an hour ago: outside the 15-minute bucket, inside the 24-hour cap.
    main._RATE[f"link021:user-day:{uid}"].extend([time.time() - 3600] * 10)
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and r.json()["retry_after_s"] > 3600
    assert fake.logins == [] and creds() == []
    # Another trader is not affected by that cap.
    d, _ = trader(main)
    assert link(d, MINE, PW_MINE).status_code == 200


def test_there_is_no_shared_ip_bucket_only_a_generous_global_cap(stack):
    """Every browser reaches the API from 127.0.0.1 through Next, so a per-IP bucket would be one global lockout."""
    main, fake, _, _ = stack
    statuses = []
    for i in range(4):
        c, _ = trader(main)
        statuses += [link(c, WRONG[i], "wrong-password").status_code for _ in range(3)]
    assert statuses == [401] * 12  # more than the old per-IP limit of 10, no one locked out
    c, _ = trader(main)
    assert link(c, MINE, PW_MINE).status_code == 200
    # The global safety cap (60 per 15 minutes, all users together) still bounds the total.
    main._RATE["link021:global:all"].extend([time.time()] * 60)
    d, _ = trader(main)
    r = link(d, OTHER, PW_OTHER)
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
    # A client-supplied forwarding header changes nothing.
    r = d.post("/api/broker/021/link", json={"ciphertext": encrypt(d, OTHER, PW_OTHER)}, headers=csrf(d) | {"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 429


def test_unlink_deletes_the_credential_and_the_session_and_returns_to_paper(stack):
    main, fake, gclient, _ = stack
    c, uid = trader(main)
    assert link(c, MINE, PW_MINE).status_code == 200
    assert MINE in gapp._linked and gapp._linked[MINE].client.logged_in
    client = gapp._linked[MINE].client
    r = unlink(c, MINE)
    assert r.status_code == 200, r.text
    assert creds() == [] and MINE not in gapp._linked and not client.logged_in
    me = c.get("/api/me").json()
    assert me["account"]["kind"] == "paper" and r.json()["active_account_id"] == me["account_id"]
    assert [a["kind"] for a in c.get("/api/broker/accounts").json()] == ["paper"]
    assert c.post("/api/broker/active", json={"account_id": MINE}, headers=csrf(c)).status_code == 404
    # The gateway now refuses that account outright.
    r = gclient.get("/v1/orders", headers={"Authorization": "Bearer k-read", "X-Account": MINE})
    assert r.status_code == 403 and r.json()["code"] == "ACCOUNT_FORBIDDEN"
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM audit_events WHERE event_type = 'ACCOUNT_UNLINKED_021'")).scalar() >= 1
    # Unlinking again is a plain not found.
    assert unlink(c, MINE).status_code == 404


def test_a_ucc_linked_to_another_user_is_refused_and_others_cannot_see_or_unlink_it(stack):
    main, fake, _, _ = stack
    a, uid_a = trader(main)
    b, uid_b = trader(main, ip="10.1.1.2")
    assert link(a, MINE, PW_MINE).status_code == 200
    r = link(b, MINE, PW_MINE)
    assert r.status_code == 409 and r.json()["code"] == "UCC_LINKED_ELSEWHERE"
    assert creds() == [(MINE, uid_a)]  # b's verified credential was not kept
    assert [x["kind"] for x in b.get("/api/broker/accounts").json()] == ["paper"]
    assert b.post("/api/broker/active", json={"account_id": MINE}, headers=csrf(b)).status_code == 404
    r = unlink(b, MINE)
    assert r.status_code == 404 and creds() == [(MINE, uid_a)]
    assert a.get("/api/me").json()["account_id"] == MINE
    # After a disconnects, b may connect the same account (and becomes its anchor).
    assert unlink(a, MINE).status_code == 200
    assert link(b, MINE, PW_MINE).status_code == 200 and creds() == [(MINE, uid_b)]
    assert [x["kind"] for x in a.get("/api/broker/accounts").json()] == ["paper"]


def test_x_account_routes_to_each_accounts_own_session(stack):
    main, fake, gclient, _ = stack
    a, _ = trader(main)
    b, _ = trader(main, ip="10.1.1.3")
    assert link(a, MINE, PW_MINE).status_code == 200
    assert link(b, OTHER, PW_OTHER).status_code == 200
    h = {"Authorization": "Bearer k-read"}
    for acct, path in ((MINE, "/v1/orders"), (OTHER, "/v1/holdings"), (ENV, "/v1/positions"), (MINE, "/v1/trades")):
        fake.reads.clear()
        assert gclient.get(path, headers=h | {"X-Account": acct}).status_code == 200
        assert fake.reads and {w for w, _ in fake.reads} == {acct}, (acct, path, fake.reads)
    assert gclient.get("/v1/orders", headers=h | {"X-Account": "NOTLINKED"}).json()["code"] == "ACCOUNT_FORBIDDEN"
    # The monitor's view covers every managed account.
    assert gclient.get("/admin/orders", headers={"Authorization": "Bearer k-audit"}).status_code == 200
    st = gapp._status()["accounts"]
    assert {v["account"] for v in st.values()} == {"ENVX***1", "SELF***4", "SELF***8"}
    assert all(MINE not in json.dumps(v) and OTHER not in json.dumps(v) for v in st.values())


def test_the_env_account_is_unchanged_and_linking_it_reuses_its_session(stack, monkeypatch):
    main, fake, gclient, _ = stack
    owner = email("owner")
    monkeypatch.setattr(settings(), "owner_emails_021", owner)
    o = new_client(main)
    assert signup(o, owner).json()["account_id"] == ENV  # the operator model works exactly as before
    rows = o.get("/api/broker/accounts").json()
    assert {r["kind"]: r["linked_by"] for r in rows} == {"paper": None, "021": "operator"}
    assert gclient.get("/v1/account", headers={"Authorization": "Bearer k-read"}).json() == {"account_id": ENV, "broker": "021", "label": "ENVX***1"}
    env_client = gapp._gw.client
    env_client.token()
    # A stranger who proves the .env account's password links it as a member; the gateway keeps ONE session for it.
    c, uid = trader(main)
    r = link(c, ENV, PW_ENV)
    assert r.status_code == 200 and r.json()["account"]["linked_by"] == "self"
    assert gapp._linked == {} and gapp._gw.client is env_client and env_client.logged_in
    assert [u for u, ok in fake.logins] == [ENV, ENV]  # the verification login replaced its own token in the same client
    assert creds() == [(ENV, uid)]
    # Disconnecting deletes the credential but never drops the .env account's session; the owner keeps the account.
    assert unlink(c, ENV).status_code == 200
    assert creds() == [] and env_client.logged_in and gapp._env_gw() is not None
    assert [x["kind"] for x in c.get("/api/broker/accounts").json()] == ["paper"]
    assert {r["kind"] for r in o.get("/api/broker/accounts").json()} == {"paper", "021"}
    # An operator-linked account cannot be "disconnected" by its listed owner through self-service.
    with session_scope() as s:
        s.execute(text("UPDATE sessions SET step_up_at = now(), step_up_method = 'pin' WHERE id IN (SELECT id FROM sessions WHERE user_id = (SELECT id FROM users WHERE lower(email)=lower(:e)))"), {"e": owner})
        s.execute(text("UPDATE users SET pin_hash='x' WHERE lower(email)=lower(:e)"), {"e": owner})
    r = unlink(o, ENV)
    assert r.status_code == 409 and r.json()["code"] == "LINK_OPERATOR_MANAGED"


def test_executor_refuses_an_approver_without_a_link(stack):
    """The executor's owner check: a self-linked trader operates their account; after unlinking they do not."""
    main, _, _, _ = stack
    c, uid = trader(main)
    assert link(c, MINE, PW_MINE).status_code == 200
    from countersign.core.models import Account, User

    with session_scope() as s:
        assert linking.may_operate_021(s, s.get(Account, MINE), s.get(User, uid))
    assert unlink(c, MINE).status_code == 200
    with session_scope() as s:
        assert not linking.may_operate_021(s, s.get(Account, MINE), s.get(User, uid))


def test_the_api_route_only_ever_handles_ciphertext(stack, monkeypatch):
    """What the API process receives and relays: an opaque ciphertext, the nonce it issued and the user id. The
    plaintext UCC and password never reach any API code path, and the API holds no 021 credential."""
    main, fake, _, _ = stack
    seen: list[dict] = []
    real = linking.gateway_link

    def spy(ciphertext, nonce, user_id):
        seen.append({"ciphertext": ciphertext, "nonce": nonce, "user_id": user_id})
        return real(ciphertext, nonce, user_id)

    monkeypatch.setattr(linking, "gateway_link", spy)
    c, uid = trader(main)
    assert link(c, MINE, PW_MINE).status_code == 200
    relay = json.dumps(seen)
    assert len(seen) == 1 and seen[0]["user_id"] == uid
    assert PW_MINE not in relay and MINE not in relay and base64.b64encode(PW_MINE.encode()).decode() not in relay
    assert set(main.LinkBody.model_fields) == {"ciphertext"}
    h = c.get("/api/health").json() if any(r.path == "/api/health" for r in main.app.routes) else {"holds_021_credentials": False}
    assert h["holds_021_credentials"] is False


def test_link_key_needs_the_gateway(stack, monkeypatch):
    main, _, _, _ = stack
    c, _ = trader(main)
    monkeypatch.setattr(settings(), "gateway021_enabled", False)
    r = c.get("/api/broker/021/link-key")
    assert r.status_code == 503 and r.json()["code"] == "GATEWAY021_OFF"
    monkeypatch.setattr(settings(), "gateway021_enabled", True)
    k = c.get("/api/broker/021/link-key").json()
    assert k["alg"] == "RSA-OAEP-256" and k["max_plaintext_bytes"] == 190
    with session_scope() as s:
        row = s.query(WebauthnChallenge).filter(WebauthnChallenge.purpose == "LINK_021", WebauthnChallenge.used_at.is_(None)).order_by(WebauthnChallenge.created_at.desc()).first()
        assert row.expires_at - datetime.now(UTC) <= timedelta(minutes=5) and row.expires_at > datetime.now(UTC)


def test_a_refused_stored_password_is_never_retried_automatically(stack):
    """After 021 refuses a linked account's stored password (changed at 021), the gateway fails closed and does not
    keep logging in with it (repeated failures could lock the account)."""
    main, fake, gclient, _ = stack
    c, _ = trader(main)
    assert link(c, MINE, PW_MINE).status_code == 200
    g = gapp._linked[MINE]
    g.client.drop_token()
    fake.passwords[MINE] = "changed-at-021"
    h = {"Authorization": "Bearer k-read", "X-Account": MINE}
    assert gclient.get("/v1/orders", headers=h).status_code == 503
    assert gclient.get("/v1/orders", headers=h).status_code == 503
    assert [ok for u, ok in fake.logins if u == MINE] == [True, False]
    assert g.client.credentials_rejected
    time.sleep(0)


# ------------------------------------------------------------------ review fixes (F1 to F9)


def _refusal_reasons(uid: str) -> list[str]:
    with session_scope() as s:
        return list(s.execute(text("SELECT data->>'reason' FROM audit_events WHERE event_type = 'ACCOUNT_LINK_021_REFUSED' AND subject_id = :u ORDER BY id"), {"u": uid}).scalars())


def _security_events(kind: str) -> list[str]:
    with session_scope() as s:
        return [r for r in s.execute(text("SELECT message || ' ' || data::text FROM security_events WHERE kind = :k ORDER BY id"), {"k": kind}).scalars()]


def _gateway_link_direct(gclient, vault: Vault, ucc: str, password: str, user_id: str, nonce: str = "n-direct"):
    """POST /admin/link straight to the gateway, as the API does, without the API's nonce bookkeeping."""
    pub = serialization.load_der_public_key(base64.b64decode(vault.public_key_spki_b64()))
    ct = base64.b64encode(pub.encrypt(json.dumps({"ucc": ucc, "password": password, "nonce": nonce, "user_id": user_id}).encode(), OAEP)).decode()
    return gclient.post("/admin/link", json={"ciphertext": ct, "nonce": nonce, "user_id": user_id}, headers={"Authorization": "Bearer k-admin"})


def test_strangers_cannot_drive_failed_logins_at_the_env_account(stack):
    """F1: the per-UCC budget (3 failed checks per 24 h across all users) is spent, then 021 is never contacted."""
    main, fake, gclient, _ = stack
    env_client = gapp._gw.client
    env_client.token()
    codes = []
    for _ in range(3):
        c, _ = trader(main)
        codes += [link(c, ENV, f"guess-{i}").json()["code"] for i in range(5)]
    failed = [ok for u, ok in fake.logins if u == ENV and not ok]
    assert len(failed) == 3
    assert codes.count("LINK_WRONG_CREDENTIALS") == 3 and codes.count("LINK_TOO_MANY_FAILURES") == 12
    # Even the right password is refused now, with a fixed message and without a 021 login.
    n = len(fake.logins)
    c, _ = trader(main)
    r = link(c, ENV.lower(), PW_ENV)
    assert r.status_code == 429 and r.json()["code"] == "LINK_TOO_MANY_FAILURES" and r.json()["message"] == "Too many failed attempts for this account, try again later."
    assert len(fake.logins) == n and creds() == []
    # The budget is durable (the gateway's database), and the .env account's own session was never marked rejected.
    assert store.link_failures(ENV, gapp.LINK_FAILURE_WINDOW_S) == 3
    assert not env_client.credentials_rejected and env_client.logged_in
    assert gclient.get("/v1/orders", headers={"Authorization": "Bearer k-read", "X-Account": ENV}).status_code == 200
    events = _security_events("BROKER_021_LINK_FAILURE_BUDGET")
    assert events and all("ENVX***1" in e and ENV not in e for e in events)
    # Another account's budget is untouched.
    assert link(c, MINE, PW_MINE).status_code == 200
    # The budget rolls: failures older than 24 hours no longer count.
    with store.session() as s:
        s.query(store.LinkFailure).update({store.LinkFailure.at: time.time() - 90000})
        s.commit()
    assert store.link_failures(ENV, gapp.LINK_FAILURE_WINDOW_S) == 0


def test_a_ucc_another_trader_holds_is_refused_without_a_021_login(stack):
    """F1: strangers cannot drive any failed login at a UCC already linked by another trader."""
    main, fake, _, _ = stack
    a, _ = trader(main)
    assert link(a, MINE, PW_MINE).status_code == 200
    b, _ = trader(main)
    codes = [link(b, MINE, f"guess-{i}").json()["code"] for i in range(4)]
    assert codes == ["UCC_LINKED_ELSEWHERE"] * 4
    assert [ok for u, ok in fake.logins if u == MINE] == [True]


def test_an_api_failure_after_the_gateway_verified_leaves_nothing_behind(stack, monkeypatch):
    """F3a: an unexpected error while attaching rolls the gateway link back (credential and session)."""
    main, fake, gclient, _ = stack
    c, uid = trader(main)

    def boom(*a, **k):
        raise RuntimeError("simulated database error")

    monkeypatch.setattr(linking, "attach_self_link", boom)
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 500 and r.json()["code"] == "LINK_FAILED" and "Nothing was kept" in r.json()["message"]
    with store.session() as s:
        assert s.query(store.PendingCredential).count() == 0
    assert creds() == [] and MINE not in gapp._linked and gapp._pending == {}
    assert [a["kind"] for a in c.get("/api/broker/accounts").json()] == ["paper"]
    assert "LINK_ATTACH_FAILED" in _refusal_reasons(uid)
    assert gclient.get("/v1/orders", headers={"Authorization": "Bearer k-read", "X-Account": MINE}).status_code == 403


def test_a_failure_after_the_confirmation_also_removes_the_confirmed_password(stack, monkeypatch):
    main, fake, _, _ = stack
    c, uid = trader(main)
    real = linking.gateway_confirm

    def confirm_then_fail(ucc, user_id):
        real(ucc, user_id)
        raise RuntimeError("simulated failure after the confirmation")

    monkeypatch.setattr(linking, "gateway_confirm", confirm_then_fail)
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 500 and r.json()["code"] == "LINK_FAILED"
    assert creds() == [] and MINE not in gapp._linked
    with session_scope() as s:
        assert s.get(BrokerLink, (MINE, uid)) is None


def test_a_pending_link_is_never_served_and_is_swept_after_the_timeout(stack):
    """F3b/e: the gateway keeps a verified password as PENDING until the API confirms; unconfirmed, it is deleted."""
    main, fake, gclient, vault = stack
    r = _gateway_link_direct(gclient, vault, MINE, PW_MINE, "user-pending")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    with store.session() as s:
        assert [(x.ucc, x.linked_user_id) for x in s.query(store.PendingCredential).all()] == [(MINE, "user-pending")]
    assert creds() == [] and store.latest_credential(MINE) is None and not store.has_credential(MINE)
    assert MINE not in gapp._linked and (MINE, "user-pending") in gapp._pending
    gapp._load_linked()  # what a gateway start loads: confirmed links only
    assert MINE not in gapp._linked
    assert gclient.get("/v1/orders", headers={"Authorization": "Bearer k-read", "X-Account": MINE}).status_code == 403
    client = gapp._pending[(MINE, "user-pending")][0]
    assert client is not None and client.logged_in
    with gapp._lock:
        assert gapp._sweep_pending(now=time.time() + store.PENDING_TTL_S + 1) == 1
    with store.session() as s:
        assert s.query(store.PendingCredential).count() == 0
    assert gapp._pending == {} and not client.logged_in
    r = gclient.post("/admin/link/confirm", json={"account_id": MINE, "user_id": "user-pending"}, headers={"Authorization": "Bearer k-admin"})
    assert r.status_code == 409 and r.json()["code"] == "LINK_EXPIRED"
    assert creds() == [] and MINE not in gapp._linked
    # A pending row past its timeout is never confirmed, even before the sweep ran.
    store.save_pending(MINE, "user-late", "sealed")
    assert store.confirm_pending(MINE, "user-late", now=time.time() + store.PENDING_TTL_S + 1) is False
    assert creds() == []


def test_two_traders_linking_the_same_new_ucc_at_once_get_one_link_and_one_clean_refusal(stack):
    """F3c: the account row is created with INSERT ... ON CONFLICT DO NOTHING, then SELECT ... FOR UPDATE."""
    main, _, _, _ = stack
    _, uid_a = trader(main)
    _, uid_b = trader(main)
    a_attached, release = threading.Event(), threading.Event()
    out: dict[str, str] = {}

    def first():
        try:
            with session_scope() as s:
                linking.attach_self_link(s, s.get(User, uid_a, with_for_update=True), OTHER)
                a_attached.set()
                release.wait(10)
            out["a"] = "ok"
        except Exception as e:  # noqa: BLE001
            out["a"] = type(e).__name__
            a_attached.set()

    def second():
        a_attached.wait(10)
        try:
            with session_scope() as s:
                linking.attach_self_link(s, s.get(User, uid_b, with_for_update=True), OTHER)
            out["b"] = "ok"
        except linking.LinkRefused as e:
            out["b"] = e.code
        except Exception as e:  # noqa: BLE001
            out["b"] = type(e).__name__

    ta, tb = threading.Thread(target=first), threading.Thread(target=second)
    ta.start()
    tb.start()
    time.sleep(0.6)  # the second insert now waits on the first transaction's uncommitted row
    release.set()
    ta.join(15)
    tb.join(15)
    assert out == {"a": "ok", "b": "UCC_LINKED_ELSEWHERE"}
    with session_scope() as s:
        assert s.get(Account, OTHER).user_id == uid_a and s.get(BrokerLink, (OTHER, uid_b)) is None


def test_a_failed_gateway_unlink_is_retried_then_surfaced_and_nothing_changes(stack, monkeypatch):
    """F3d: never swallow a failed gateway unlink; the link and the stored password stay consistent."""
    main, _, _, _ = stack
    c, uid = trader(main)
    assert link(c, MINE, PW_MINE).status_code == 200
    real = linking.gateway_unlink
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        raise linking.LinkRefused(503, "GATEWAY021_UNAVAILABLE", "down")

    monkeypatch.setattr(linking, "gateway_unlink", flaky)
    r = unlink(c, MINE)
    assert r.status_code == 503 and r.json()["code"] == "GATEWAY021_UNAVAILABLE" and "Nothing was changed" in r.json()["message"]
    assert calls["n"] == 2  # tried twice
    assert creds() == [(MINE, uid)] and MINE in gapp._linked
    with session_scope() as s:
        assert s.get(BrokerLink, (MINE, uid)) is not None
        assert s.execute(text("SELECT count(*) FROM audit_events WHERE event_type = 'ACCOUNT_UNLINK_021_FAILED' AND subject_id = :u"), {"u": uid}).scalar() == 1
    # One transient failure is absorbed by the retry.
    calls["n"] = 0

    def once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise linking.LinkRefused(503, "GATEWAY021_UNAVAILABLE", "down")
        return real(*a, **k)

    monkeypatch.setattr(linking, "gateway_unlink", once)
    assert unlink(c, MINE).status_code == 200 and creds() == [] and MINE not in gapp._linked


def test_a_failed_rollback_is_surfaced_and_the_pending_link_still_expires(stack, monkeypatch):
    main, _, _, _ = stack
    c, uid = trader(main)
    monkeypatch.setattr(linking, "attach_self_link", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(linking, "gateway_unlink", lambda *a, **k: (_ for _ in ()).throw(linking.LinkRefused(503, "GATEWAY021_UNAVAILABLE", "down")))
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 503 and r.json()["code"] == "LINK_ROLLBACK_FAILED" and "two minutes" in r.json()["message"]
    assert _security_events("BROKER_021_LINK_ROLLBACK_FAILED")
    assert creds() == [] and MINE not in gapp._linked  # pending only: never served
    with gapp._lock:
        gapp._sweep_pending(now=time.time() + store.PENDING_TTL_S + 1)
    with store.session() as s:
        assert s.query(store.PendingCredential).count() == 0
    assert gapp._pending == {}


def test_link_refusal_reasons_are_audited_and_repeated_wrong_passwords_alert(stack):
    """F5: reasons are stored under "reason" (the scrubber redacts "code"), pre-relay refusals are audited too, and
    every third wrong-credential refusal in 24 hours raises a security event for the user."""
    main, _, _, _ = stack
    c, uid = trader(main)
    for i in range(3):
        assert link(c, WRONG[i], "wrong").json()["code"] == "LINK_WRONG_CREDENTIALS"
    r = c.post("/api/broker/021/link", json={"ciphertext": "A" * 64}, headers=csrf(c))
    assert r.status_code == 400 and r.json()["code"] == "LINK_NONCE_INVALID"
    with session_scope() as s:
        s.execute(text("UPDATE sessions SET step_up_at = now() - interval '1 hour' WHERE user_id=:u"), {"u": uid})
    assert link(c, MINE, PW_MINE).json()["code"] == "STEP_UP_REQUIRED"
    assert _refusal_reasons(uid) == ["LINK_WRONG_CREDENTIALS"] * 3 + ["LINK_NONCE_INVALID", "STEP_UP_REQUIRED"]
    with session_scope() as s:
        redacted = s.execute(text("SELECT count(*) FROM audit_events WHERE event_type = 'ACCOUNT_LINK_021_REFUSED' AND subject_id = :u AND data::text LIKE '%redacted%'"), {"u": uid}).scalar()
    assert redacted == 0
    events = [e for e in _security_events("BROKER_021_LINK_WRONG_CREDENTIALS") if uid in e]
    assert len(events) == 1 and "3 attempts" in events[0]


def test_owner_access_is_limited_to_the_current_env_account(stack, monkeypatch):
    """F6: a listed owner operates only the account whose id is the gateway's current .env UCC; an 'operator' row
    left by an earlier .env UCC grants nothing, is switched to 'self', and the join-with-password exception does not
    apply to it."""
    main, _, _, _ = stack
    owner = email("owner")
    monkeypatch.setattr(settings(), "owner_emails_021", owner)
    o = new_client(main)
    assert signup(o, owner).json()["account_id"] == ENV
    with session_scope() as s:
        oid = s.execute(text("SELECT id FROM users WHERE lower(email) = lower(:e)"), {"e": owner}).scalar()
        s.add(Account(id=OLD, user_id=oid, label="old", segments=["EQ"], trading_enabled=True, broker_kind="021", linked_via="operator"))
    with session_scope() as s:
        u = s.get(User, oid)
        assert linking.may_operate_021(s, s.get(Account, ENV), u)
        assert not linking.may_operate_021(s, s.get(Account, OLD), u) and not linking.can_operate(s, oid, OLD)
    assert OLD not in {a["account_id"] for a in o.get("/api/broker/accounts").json()}
    # Signing in again retires the stale operator row.
    assert new_client(main).post("/api/auth/login", json={"email": owner, "password": PASSWORD}).status_code == 200
    with session_scope() as s:
        assert s.get(Account, OLD).linked_via == "self" and s.get(Account, ENV).linked_via == "operator"
    # A row still marked 'operator' that is not the .env account: a second trader is refused like any other UCC.
    _, uid_a = trader(main)
    _, uid_b = trader(main)
    with session_scope() as s:
        s.execute(text("UPDATE accounts SET linked_via = 'operator' WHERE id = :a"), {"a": OLD})
        s.add(BrokerLink(account_id=OLD, user_id=uid_a))
    with session_scope() as s, pytest.raises(linking.LinkRefused) as e:
        linking.attach_self_link(s, s.get(User, uid_b, with_for_update=True), OLD)
    assert e.value.code == "UCC_LINKED_ELSEWHERE"
    # The current .env account keeps the exception: proving its password joins it as a member.
    with session_scope() as s:
        linking.attach_self_link(s, s.get(User, uid_b, with_for_update=True), ENV.lower())
        assert s.get(BrokerLink, (ENV, uid_b)) is not None


def test_uccs_are_normalized_everywhere(stack, monkeypatch):
    """F7: CS_021_UCC, X-Account, the linked UCC and the API's comparisons all use the trimmed upper-case UCC."""
    main, _, gclient, _ = stack
    assert GatewaySettings(ucc="  envx0001 ", password="x").ucc == ENV
    c, uid = trader(main)
    assert link(c, "  self1234 ", PW_MINE).json()["active_account_id"] == MINE
    assert creds() == [(MINE, uid)]
    assert gclient.get("/v1/orders", headers={"Authorization": "Bearer k-read", "X-Account": "self1234"}).status_code == 200
    assert linking.norm_ucc(" envx0001\n") == ENV and linking.is_env_account("envx0001")
    monkeypatch.setattr(linking, "gateway_account_id", lambda refresh=False: " envx0001 ")
    assert linking.is_env_account(ENV)


def test_021_login_errors_are_fixed_messages_with_only_the_http_status(stack):
    """F8: 021's own error text never reaches the API, the status or a browser."""
    main, fake, gclient, _ = stack
    c, _ = trader(main)
    fake.login_status = 400
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 422 and r.json()["code"] == "LINK_REFUSED" and r.json()["message"] == "021 refused the login (HTTP 400). Nothing was stored."
    fake.login_status = 401
    r = link(c, OTHER, PW_OTHER)
    assert r.json()["code"] == "LINK_WRONG_CREDENTIALS" and "raw-021-text" not in r.text
    # The .env account's own login (daily re-login) gives the same fixed text.
    gapp._gw.client.drop_token()
    fake.login_status = 403
    r = gclient.get("/v1/orders", headers={"Authorization": "Bearer k-read", "X-Account": ENV})
    assert r.status_code == 503 and r.json()["message"] == login_failure(403) and "raw-021-text" not in r.text
    assert "raw-021-text" not in json.dumps(gapp._status(), default=str)
    assert login_failure(500) == "021 could not process the login (HTTP 500)." and login_failure(429).endswith("(HTTP 429).")


def test_status_names_accounts_by_an_hmac_with_a_gateway_only_key(stack):
    """F9: /health and the heartbeat never carry a plain hash of a UCC; the API resolves its own accounts by asking
    the gateway (read key)."""
    main, _, gclient, vault = stack
    c, _ = trader(main)
    assert link(c, MINE, PW_MINE).status_code == 200
    keys = set(gapp._status()["accounts"])
    plain = {hashlib.sha256(u.encode()).hexdigest()[:16] for u in (ENV, MINE)}
    assert not keys & plain
    assert keys == {hmac.new(vault.status_key(), u.encode(), hashlib.sha256).hexdigest()[:16] for u in (ENV, MINE)}
    assert main._gateway_account(gapp._status(), MINE)["account"] == "SELF***4"
    r = gclient.get("/v1/account-key", headers={"Authorization": "Bearer k-read", "X-Account": OTHER})
    assert r.status_code == 403  # only for accounts it serves
    assert gclient.get("/v1/account-key", headers={"X-Account": MINE}).status_code == 401
    assert (vault.dir / vault_mod.STATUS_KEY_FILE).exists()


def test_key_files_are_restricted_to_the_current_user(tmp_path, monkeypatch, caplog):
    """F4: on Windows the key files lose inherited ACL entries and only the current user gets full control (icacls,
    argument list, no shell); on POSIX they are chmod 0o600. A failure only logs a warning naming the file."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kw):
        calls.append((list(args), kw))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(vault_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(vault_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("USERNAME", "trader one")
    v = Vault(tmp_path)
    v.ensure_keys()
    assert sorted(os.path.basename(a[1]) for a, _ in calls) == sorted(vault_mod.KEY_FILES)
    for args, kw in calls:
        assert args[0] == "icacls" and args[2:] == ["/inheritance:r", "/grant:r", "trader one:F"]
        assert kw.get("shell") is False
    calls.clear()
    assert v.restrict_files() == {name: True for name in vault_mod.KEY_FILES} and len(calls) == 3
    monkeypatch.setattr(vault_mod.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 5, b"", b"denied"))
    with caplog.at_level("WARNING"):
        assert vault_mod.restrict_file(tmp_path / vault_mod.STORE_KEY_FILE) is False
    assert "gateway021_store.key" in caplog.text and "denied" not in caplog.text
    secret = (tmp_path / vault_mod.STORE_KEY_FILE).read_text().strip()
    assert secret not in caplog.text
    chmods: list[tuple[str, int]] = []
    monkeypatch.setattr(vault_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(vault_mod.os, "chmod", lambda p, m: chmods.append((str(p), m)))
    assert vault_mod.restrict_file(tmp_path / vault_mod.LINK_KEY_FILE) is True and chmods[0][1] == 0o600


def test_the_executor_learns_the_env_account_with_its_write_key(monkeypatch):
    """F6 needs the gateway's .env UCC in the executor too, which holds only the write key (no read key)."""
    seen: list[str] = []

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"account_id": " envx0001 "}

    def fake_get(url, headers=None, timeout=None):
        seen.append(headers["Authorization"])
        return R()

    cfg = settings()
    for name, val in (("gateway021_enabled", True), ("broker_read_key", ""), ("broker_write_key", "k-write"), ("broker_admin_key", "")):
        monkeypatch.setattr(cfg, name, val)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setitem(linking._ucc, "value", None)
    assert linking.gateway_account_id(refresh=True) == ENV and seen == ["Bearer k-write"]
    linking._ucc.update(value=None, at=0.0)
