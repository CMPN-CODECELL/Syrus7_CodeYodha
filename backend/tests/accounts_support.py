"""Shared helpers for the account tests: an API TestClient with the broker admin stubbed, unique test identities,
and a software passkey (its own key and credential id, never the Attack Lab's)."""

from __future__ import annotations

import secrets
import uuid

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from countersign.auth import virtual_authenticator as vauth
from countersign.core import proposals
from countersign.core.approval_crypto import b64url
from countersign.core.db import session_scope
from countersign.core.models import Account, DeviceCredential, User
from countersign.core.parser import parse
from tests.conftest import TRANSACTIONAL, FakeGateway, set_tick

TEST_DOMAIN = "test.countersign.invalid"
PASSWORD = "correct horse battery 42"


class FakeAdmin:
    """Stands in for the paper broker's admin API and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_account(self, account_id: str) -> dict:
        self.calls.append(("create", account_id))
        return {"ok": True, "account_id": account_id}

    def reset_account(self, account_id: str) -> dict:
        self.calls.append(("reset", account_id))
        return {"ok": True, "account_id": account_id, "orders_deleted": 0}

    def set_fault(self, mode: str, count: int = 1) -> dict:
        self.calls.append(("fault", mode))
        return {"mode": mode, "remaining": 0}

    def fault_state(self) -> dict:
        return {"mode": "none", "remaining": 0}

    def replenish(self, account_id: str) -> dict:
        self.calls.append(("replenish", account_id))
        return {"ok": True, "account_id": account_id}


def api_fixture(monkeypatch):
    """Body of the `api` fixture in conftest.py: yields (api module, FakeAdmin) and removes test users after."""
    from countersign.api import main

    main._RATE.clear()
    main._FAILS.clear()
    main._PUBLIC.update(at=0.0, body=None)
    admin = FakeAdmin()
    monkeypatch.setattr(main, "BrokerAdmin", lambda *a, **k: admin)
    monkeypatch.setattr(main, "broker_health", lambda: (True, 1))
    yield main, admin
    _cleanup_test_users()


def _cleanup_test_users() -> None:
    with session_scope() as s:
        s.execute(text("TRUNCATE " + ", ".join(TRANSACTIONAL) + " CASCADE"))
        ids = list(s.execute(select(User.id).where(User.email.like(f"%@{TEST_DOMAIN}"))).scalars())
        if ids:
            s.execute(text("DELETE FROM broker_links WHERE user_id = ANY(:ids) OR account_id IN (SELECT id FROM accounts WHERE user_id = ANY(:ids))"), {"ids": ids})
            s.execute(text("DELETE FROM account_members WHERE user_id = ANY(:ids) OR account_id IN (SELECT id FROM accounts WHERE user_id = ANY(:ids))"), {"ids": ids})
            for tbl in ("sessions", "device_credentials", "webauthn_challenges", "accounts"):
                s.execute(text(f"DELETE FROM {tbl} WHERE user_id = ANY(:ids)"), {"ids": ids})
            s.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids})


def new_client(main, ip: str = "127.0.0.1") -> TestClient:
    # No context manager: the API startup hook (bootstrap, mandate engine thread) is not needed here. The peer is a
    # loopback address like the real scripts' (the API listens on loopback only); pass ip= to act as another client.
    return TestClient(main.app, client=(ip, 50000))


def link_totp(user_id: str) -> str:
    """Links and confirms an authenticator app for a user directly, as the setup and verify endpoints would."""
    from countersign.auth import factors

    with session_scope() as s:
        u = s.get(User, user_id)
        secret = factors.totp_setup(u)[1]
        u.totp_secret_enc, u.totp_pending_enc, u.totp_confirmed = u.totp_pending_enc, None, True
    return secret


def expire_login_locks(main, email: str) -> None:
    """Moves every password lock on this email into the past, as if its wait had elapsed."""
    for k, (n, _, last) in list(main._FAILS.items()):
        if k[0] == email:
            main._FAILS[k] = (n, 0.0, last)


def email(tag: str = "trader") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@{TEST_DOMAIN}"


def csrf(c: TestClient) -> dict:
    return {"X-CSRF-Token": c.cookies.get("cs_csrf") or ""}


def signup(c: TestClient, addr: str | None = None, password: str = PASSWORD, name: str = "Asha Rao"):
    return c.post("/api/auth/signup", json={"display_name": name, "email": addr or email(), "password": password})


def signed_up(main) -> tuple[TestClient, dict]:
    c = new_client(main)
    r = signup(c)
    assert r.status_code == 201, r.text
    return c, r.json()


def propose_for(user_id: str, account_id: str, text_: str = "buy 5 INFY at 1860") -> str:
    from countersign.core.verifier import AccountState

    set_tick("NSE:EQ:INFY", 186000)
    acct = AccountState.load(FakeGateway(), account_id)
    with session_scope() as s:
        out = proposals.create_order(s, parse(text_).order, user_text=text_, account=acct, user_id=user_id, engine="deterministic", origin="test")
        assert out.proposal is not None, out.clarify
        return out.proposal.id


def account_of(user_id: str) -> str:
    with session_scope() as s:
        return s.execute(select(Account.id).where(Account.user_id == user_id)).scalar_one()


class SoftPasskey(vauth.VirtualAuthenticator):
    """A software platform authenticator with a fresh key, standing in for a trader's real passkey."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.sign_count = 0
        self._cid = secrets.token_bytes(16)

    @property
    def credential_id(self) -> str:
        return b64url(self._cid)

    def register(self, user_id: str, *, is_virtual: bool = False) -> str:
        with session_scope() as s:
            d = DeviceCredential(user_id=user_id, credential_id=self.credential_id, public_key=self.cose_public_key(), sign_count=0, transports=["internal"], label="Test passkey", is_virtual=is_virtual)
            s.add(d)
            s.flush()
            return d.id

    def login_assertion(self, challenge: bytes, user_id: str) -> dict:
        a = self.assert_challenge(challenge)
        with session_scope() as s:
            a["response"]["userHandle"] = b64url(s.get(User, user_id).webauthn_user_handle)
        return a
