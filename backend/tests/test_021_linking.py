"""Self-service 021 linking: the browser encrypts {ucc, password, nonce, user_id} to the gateway's RSA key, the API relays
only the ciphertext (with the nonce it issued and the caller's user id) and the gateway verifies the password with one
021 login before storing it sealed. Runs the real API and the real gateway app in-process; 021 itself is a fake
(httpx MockTransport). No test calls the live sandbox."""

from __future__ import annotations

import base64
import json
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
from countersign.broker021.settings import GatewaySettings
from countersign.broker021.vault import OAEP, Vault
from countersign.core.broker_client import _kinds
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import WebauthnChallenge
from tests.accounts_support import csrf, email, new_client, signup
from tests.test_021_gateway import FakeLookup

ENV = "ENVX0001"  # the account configured in .env (operator-linked)
MINE = "SELF1234"  # a trader's own 021 account
OTHER = "SELF5678"
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
    monkeypatch.setattr(gapp, "_transport", transport)
    monkeypatch.setattr(gapp, "_start_poller", lambda g: None)
    monkeypatch.setattr(gapp, "_ensure_market", lambda: None)
    gclient = TestClient(gapp.app, client=("127.0.0.1", 50000))
    monkeypatch.setattr(linking, "gateway_http", lambda: _NoLifespan(gclient))
    monkeypatch.setattr(linking, "gateway_account_id", lambda refresh=False: ENV)
    main._LINK_KEY.update(value=None, at=0.0)
    _wipe()
    yield main, fake, gclient, vault
    _wipe()


def _wipe() -> None:
    with store.session() as s:
        s.query(store.Credential).delete(synchronize_session=False)
        s.commit()
    with session_scope() as s:
        for a in (ENV, MINE, OTHER):
            s.execute(text("DELETE FROM broker_links WHERE account_id=:a"), {"a": a})
            s.execute(text("DELETE FROM account_members WHERE account_id=:a"), {"a": a})
            s.execute(text("UPDATE users SET active_account_id=NULL WHERE active_account_id=:a"), {"a": a})
            s.execute(text("DELETE FROM accounts WHERE id=:a"), {"a": a})
    for a in (ENV, MINE, OTHER):
        _kinds.pop(a, None)


def trader(main, ip: str = "127.0.0.1", addr: str | None = None) -> tuple[TestClient, str]:
    """A signed-up trader whose first PIN counts as a fresh step-up (as in onboarding)."""
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
    c, _ = trader(main)
    codes = [link(c, MINE, "wrong-password").status_code for _ in range(5)]
    assert codes == [401] * 5
    r = link(c, MINE, PW_MINE)
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and r.json()["retry_after_s"] > 0
    assert len(fake.logins) == 5 and creds() == []


def test_link_attempts_are_rate_limited_per_ip(stack):
    main, fake, _, _ = stack
    statuses = []
    for _ in range(3):
        c, _ = trader(main, ip="10.9.8.7")
        statuses += [link(c, MINE, "wrong-password").status_code for _ in range(4)]
    assert statuses.count(401) == 10 and statuses.count(429) == 2


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
