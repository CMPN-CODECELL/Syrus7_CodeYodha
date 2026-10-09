"""Which Countersign users operate the real 021 sandbox account (hackathon linking model).

The 021 account id is its UCC, which only the gateway knows from configuration; the API learns it from the gateway's
GET /v1/account with its read key (never the password or token). Users whose email is listed in CS_021_OWNER_EMAILS
are linked on sign-up and sign-in and land on the 021 account; they can switch back to their paper account.
Self-service linking of any 021 UCC is future scope. This module holds no credential."""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from countersign.core import audit
from countersign.core.config import settings
from countersign.core.models import Account, AccountMember, User

_ucc: dict = {"value": None, "at": 0.0}


def mask(account_id: str) -> str:
    return (account_id[:4] + "***" + account_id[-1:]) if len(account_id) > 5 else "***"


def gateway_account_id(refresh: bool = False) -> str | None:
    """The 021 account id served by the gateway (cached for 10 minutes), or None when the gateway is not running."""
    if not settings().gateway021_enabled:
        return None
    if not refresh and _ucc["value"] and time.time() - _ucc["at"] < 600:
        return _ucc["value"]
    try:
        import httpx

        r = httpx.get(settings().gateway021_url + "/v1/account", headers={"Authorization": f"Bearer {settings().broker_read_key}"}, timeout=3.0)
        acct = r.json().get("account_id") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return _ucc["value"]
    if acct:
        _ucc.update(value=acct, at=time.time())
    return acct


def is_owner(user: User) -> bool:
    return bool(user.email) and user.email.strip().lower() in settings().owners_021


def can_operate(s: Session, user_id: str, account_id: str) -> bool:
    """Paper accounts: their owner. The 021 account: a linked user whose email is STILL listed in CS_021_OWNER_EMAILS
    (the list is authoritative: removing an email revokes access on the next request)."""
    acct = s.get(Account, account_id)
    if acct is None:
        return False
    linked = acct.user_id == user_id or s.get(AccountMember, (account_id, user_id)) is not None
    if not linked:
        return False
    if acct.broker_kind == "021":
        u = s.get(User, user_id)
        return u is not None and is_owner(u)
    return True


def user_accounts(s: Session, user: User) -> list[Account]:
    own = list(s.execute(select(Account).where(Account.user_id == user.id)).scalars())
    shared_ids = list(s.execute(select(AccountMember.account_id).where(AccountMember.user_id == user.id)).scalars())
    shared = [a for a in (s.get(Account, i) for i in shared_ids) if a is not None and a not in own]
    owner_now = is_owner(user)
    usable = [a for a in own + shared if a.broker_kind != "021" or owner_now]
    return sorted(usable, key=lambda a: (a.broker_kind != "paper", a.id))


def active_account(s: Session, user: User) -> Account | None:
    """The account a trader is acting on: their chosen active account if they may still operate it, else their own
    paper account (never someone else's account, never a shared fallback)."""
    if user.active_account_id and can_operate(s, user.id, user.active_account_id):
        acct = s.get(Account, user.active_account_id)
        if acct is not None:
            return acct
    accts = user_accounts(s, user)
    return accts[0] if accts else None


def link_on_sign_in(s: Session, user: User) -> str | None:
    """Links an owner (by email) to the 021 account and makes it their active account. Returns the account id linked,
    or None. Never raises: a gateway that is down simply leaves the trader on their paper account."""
    try:
        if not is_owner(user):
            return None
        ucc = gateway_account_id()
        if not ucc:
            return None
        acct = s.get(Account, ucc)
        if acct is None:
            acct = Account(id=ucc, user_id=user.id, label="021 sandbox account", segments=["EQ", "FO"], trading_enabled=True, broker_kind="021")
            s.add(acct)
            s.flush()
            audit.record(s, "system", "ACCOUNT_LINKED_021", "user", user.id, {"account": mask(ucc), "role": "owner", "model": "CS_021_OWNER_EMAILS"})
        elif acct.broker_kind != "021":
            return None
        elif acct.user_id != user.id:
            anchor = s.get(User, acct.user_id)
            if anchor is None or not is_owner(anchor):
                # The recorded owner is no longer listed (for example a test identity): the listed user takes over
                # the anchor; the previous one keeps a member row that grants nothing while it is unlisted.
                previous = acct.user_id
                acct.user_id = user.id
                stale = s.get(AccountMember, (ucc, user.id))
                if stale is not None:
                    s.delete(stale)
                if s.get(AccountMember, (ucc, previous)) is None:
                    s.add(AccountMember(account_id=ucc, user_id=previous))
                s.flush()
                audit.record(s, "system", "ACCOUNT_LINKED_021", "user", user.id, {"account": mask(ucc), "role": "owner", "model": "CS_021_OWNER_EMAILS", "took_over_from_unlisted": True})
            elif s.get(AccountMember, (ucc, user.id)) is None:
                s.add(AccountMember(account_id=ucc, user_id=user.id))
                s.flush()
                audit.record(s, "system", "ACCOUNT_LINKED_021", "user", user.id, {"account": mask(ucc), "role": "member", "model": "CS_021_OWNER_EMAILS"})
        if user.active_account_id != ucc:
            user.active_account_id = ucc
        return ucc
    except Exception:  # noqa: BLE001
        return None


def account_view(s: Session, a: Account, active_id: str | None) -> dict:
    return {
        "account_id": a.id,
        "kind": a.broker_kind,
        "label": f"021 sandbox {mask(a.id)}" if a.broker_kind == "021" else a.label,
        "masked_id": mask(a.id) if a.broker_kind == "021" else a.id,
        "active": a.id == active_id,
        "status": "ready" if a.trading_enabled else "disabled",
    }
