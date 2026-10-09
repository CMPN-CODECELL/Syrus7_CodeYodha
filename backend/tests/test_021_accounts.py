"""Hackathon linking model for the 021 account: emails in CS_021_OWNER_EMAILS are linked on sign-up and sign-in and
land on the 021 account; everyone keeps their own paper account and can switch; nobody else ever sees the 021
account. The 021 id is masked for display."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from countersign.broker021 import linking
from countersign.core.broker_client import _kinds
from countersign.core.config import settings
from countersign.core.db import session_scope
from tests.accounts_support import PASSWORD, csrf, email, new_client, signup

ACCT = "HACK0000"


@pytest.fixture
def owners(api, monkeypatch):
    main, admin = api
    addrs = [email("owner"), email("partner")]
    monkeypatch.setattr(settings(), "gateway021_enabled", True)
    monkeypatch.setattr(settings(), "owner_emails_021", ",".join(a.upper() for a in addrs))
    monkeypatch.setattr(linking, "gateway_account_id", lambda refresh=False: ACCT)
    with session_scope() as s:
        s.execute(text("DELETE FROM account_members WHERE account_id=:a"), {"a": ACCT})
        s.execute(text("DELETE FROM accounts WHERE id=:a"), {"a": ACCT})
    yield main, admin, addrs
    with session_scope() as s:
        s.execute(text("DELETE FROM account_members WHERE account_id=:a"), {"a": ACCT})
        s.execute(text("DELETE FROM accounts WHERE id=:a"), {"a": ACCT})
    _kinds.pop(ACCT, None)


def test_owner_lands_on_the_021_account_and_can_switch_to_paper(owners):
    main, _, (owner, _) = owners
    c = new_client(main)
    r = signup(c, owner)
    assert r.status_code == 201, r.text
    me = r.json()
    assert me["account_id"] == ACCT and me["account"]["kind"] == "021" and me["account"]["label"] == "021 sandbox HACK***0"
    lst = c.get("/api/broker/accounts").json()
    kinds = {a["kind"]: a for a in lst}
    assert set(kinds) == {"paper", "021"} and kinds["021"]["active"] and not kinds["paper"]["active"]
    assert kinds["021"]["masked_id"] == "HACK***0" and kinds["021"]["label"] == "021 sandbox HACK***0"
    assert kinds["021"]["status"]["market_data"] == "live" and kinds["021"]["status"]["connected"] is False
    paper = kinds["paper"]["account_id"]
    r = c.post("/api/broker/active", json={"account_id": paper}, headers=csrf(c))
    assert r.status_code == 200 and r.json()["active_account_id"] == paper
    assert c.get("/api/me").json()["account_id"] == paper
    # Owners land on the 021 account at every sign-in (the hackathon linking model).
    c2 = new_client(main)
    r = c2.post("/api/auth/login", json={"email": owner, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["account_id"] == ACCT


def test_a_second_owner_operates_the_same_021_account_as_a_member(owners):
    main, _, (owner, partner) = owners
    c1 = new_client(main)
    assert signup(c1, owner).json()["account_id"] == ACCT
    c2 = new_client(main)
    me = signup(c2, partner).json()
    assert me["account_id"] == ACCT
    with session_scope() as s:
        n = s.execute(text("SELECT count(*) FROM account_members WHERE account_id=:a"), {"a": ACCT}).scalar()
    assert n == 1


def test_non_owners_never_see_or_switch_to_the_021_account(owners):
    main, _, _ = owners
    c = new_client(main)
    me = signup(c, email("stranger")).json()
    assert me["account"]["kind"] == "paper"
    lst = c.get("/api/broker/accounts").json()
    assert [a["kind"] for a in lst] == ["paper"]
    r = c.post("/api/broker/active", json={"account_id": ACCT}, headers=csrf(c))
    assert r.status_code == 404 and c.get("/api/me").json()["account"]["kind"] == "paper"


def test_gateway_down_leaves_owners_on_paper(owners, monkeypatch):
    main, _, (owner, _) = owners
    monkeypatch.setattr(linking, "gateway_account_id", lambda refresh=False: None)
    c = new_client(main)
    me = signup(c, owner).json()
    assert me["account"]["kind"] == "paper"


def test_demo_reset_refuses_the_021_account(owners):
    main, admin, (owner, _) = owners
    c = new_client(main)
    signup(c, owner)
    r = c.post("/api/demo/reset", headers=csrf(c))
    assert r.status_code == 409 and r.json()["code"] == "NOT_RESETTABLE"
    assert not any(call[0] == "reset" for call in admin.calls)


def test_health_and_status_never_expose_021_credentials(owners):
    main, _, _ = owners
    c = new_client(main)
    signup(c, email("viewer"))
    h = c.get("/api/health").json()
    assert h["holds_021_credentials"] is False and h["gateway021_enabled"] is True
    st = c.get("/api/broker/status").json()
    assert st["active_kind"] == "paper" and "password" not in str(st).lower()
    sec = c.get("/api/security/status").json()
    assert sec["broker"]["kind"] == "paper" and "021 live prices" in sec["feed"]["mode"]


def test_removing_an_email_from_the_owner_list_revokes_021_access(owners, monkeypatch):
    main, _, (owner, partner) = owners
    c = new_client(main)
    assert signup(c, owner).json()["account_id"] == ACCT
    monkeypatch.setattr(settings(), "owner_emails_021", partner)
    me = c.get("/api/me").json()
    assert me["account"]["kind"] == "paper"
    assert [a["kind"] for a in c.get("/api/broker/accounts").json()] == ["paper"]
    assert c.post("/api/broker/active", json={"account_id": ACCT}, headers=csrf(c)).status_code == 404


def test_a_listed_owner_takes_over_from_an_unlisted_anchor(owners, monkeypatch):
    main, _, (owner, partner) = owners
    c1 = new_client(main)
    assert signup(c1, owner).json()["account_id"] == ACCT  # owner anchors the account
    monkeypatch.setattr(settings(), "owner_emails_021", partner)  # owner is removed from the list
    c2 = new_client(main)
    assert signup(c2, partner).json()["account_id"] == ACCT
    with session_scope() as s:
        anchor = s.execute(text("SELECT u.email FROM accounts a JOIN users u ON u.id = a.user_id WHERE a.id=:a"), {"a": ACCT}).scalar()
    assert anchor == partner.lower()
    assert c1.get("/api/me").json()["account"]["kind"] == "paper"
