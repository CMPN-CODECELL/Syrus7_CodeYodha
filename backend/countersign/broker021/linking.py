"""Which Countersign users operate which 021 accounts. This module holds no credential.

Two ways to be linked:
  - operator model (the .env account): the 021 account id is its UCC, which only the gateway knows from configuration;
    the API learns it from the gateway's GET /v1/account with its read key (never the password or token). Users whose
    email is listed in CS_021_OWNER_EMAILS are linked on sign-up and sign-in and land on it (accounts.linked_via =
    'operator'); the list is authoritative. An account counts as operator-linked ONLY while its id equals the
    gateway's current .env UCC (normalized): a listed owner never operates any other 021 row, whatever its linked_via
    says, and rows left as 'operator' by an earlier .env UCC are switched to 'self'.
  - self-service: a trader enters their own UCC and password, encrypted in the browser to the gateway's public key; the
    API relays only the ciphertext (with the nonce it issued and the user id) to the gateway, which verifies them with
    one 021 login and keeps the password sealed. The API then records a broker_links row (the grant) and attaches the
    account (accounts.linked_via = 'self') and confirms the link to the gateway, which deletes any link it does not
    confirm within two minutes. Disconnecting deletes the row and the gateway's stored credential.
A trader may operate a 021 account while linked to it (owner or member row) AND either holds a broker_links row for it
or is a listed owner of the operator-linked account."""

from __future__ import annotations

import time

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from countersign.core import audit
from countersign.core.config import settings
from countersign.core.models import Account, AccountMember, BrokerLink, User

_ucc: dict = {"value": None, "at": 0.0}


def mask(account_id: str) -> str:
    return (account_id[:4] + "***" + account_id[-1:]) if len(account_id) > 5 else "***"


def norm_ucc(v: object) -> str:
    """The one spelling of a UCC (same rule as the gateway's settings): trimmed and upper-case."""
    return str(v or "").strip().upper()


def gateway_account_id(refresh: bool = False) -> str | None:
    """The 021 account id served by the gateway (cached for 10 minutes), or None when the gateway is not running."""
    if not settings().gateway021_enabled:
        return None
    if not refresh and _ucc["value"] and time.time() - _ucc["at"] < 600:
        return _ucc["value"]
    if not refresh and time.time() - _ucc.get("failed_at", 0.0) < 15:
        return _ucc["value"]  # the gateway did not answer moments ago: owner checks do not wait on it again
    try:
        import httpx

        # Any broker key opens GET /v1/account: the API holds the read key, the executor only the write key.
        key = settings().broker_read_key or settings().broker_write_key or settings().broker_admin_key
        r = httpx.get(settings().gateway021_url + "/v1/account", headers={"Authorization": f"Bearer {key}"}, timeout=3.0)
        acct = norm_ucc(r.json().get("account_id")) if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        _ucc["failed_at"] = time.time()
        return _ucc["value"]
    if acct:
        _ucc.update(value=acct, at=time.time())
    return acct


def is_owner(user: User) -> bool:
    return bool(user.email) and user.email.strip().lower() in settings().owners_021


def self_linked(s: Session, account_id: str, user_id: str) -> bool:
    return s.get(BrokerLink, (account_id, user_id)) is not None


def is_env_account(account_id: str) -> bool:
    """True only for the gateway's CURRENT .env account (normalized ids); False when the gateway is not known."""
    env = gateway_account_id()
    return bool(env) and norm_ucc(account_id) == norm_ucc(env)


def retire_stale_operator_rows(s: Session) -> int:
    """021 rows marked operator-linked (or NULL) whose id is not the gateway's current .env UCC become 'self': the owner
    list applies only to the current .env account. No-op while the gateway's account is unknown."""
    env = gateway_account_id()
    if not env:
        return 0
    res = s.execute(update(Account).where(Account.broker_kind == "021", (Account.linked_via.is_(None)) | (Account.linked_via != "self"), Account.id != norm_ucc(env)).values(linked_via="self"))
    return int(res.rowcount or 0)


def may_operate_021(s: Session, acct: Account, user: User | None) -> bool:
    """For a 021 account the trader is already linked to (owner or member row): their own self-service link, or (only
    for the gateway's current .env account) an email STILL listed in CS_021_OWNER_EMAILS. linked_via is never enough
    on its own. Used by the API on every request and by the executor before sending."""
    if user is None:
        return False
    if self_linked(s, acct.id, user.id):
        return True
    return is_owner(user) and is_env_account(acct.id)


def can_operate(s: Session, user_id: str, account_id: str) -> bool:
    """Paper accounts: their owner. A 021 account: a linked user who still holds a self-service link to it, or whose
    email is STILL listed in CS_021_OWNER_EMAILS for the operator-linked account (the list is authoritative: removing
    an email revokes access on the next request)."""
    acct = s.get(Account, account_id)
    if acct is None:
        return False
    linked = acct.user_id == user_id or s.get(AccountMember, (account_id, user_id)) is not None
    if not linked:
        return False
    if acct.broker_kind == "021":
        return may_operate_021(s, acct, s.get(User, user_id))
    return True


def user_accounts(s: Session, user: User) -> list[Account]:
    own = list(s.execute(select(Account).where(Account.user_id == user.id)).scalars())
    shared_ids = list(s.execute(select(AccountMember.account_id).where(AccountMember.user_id == user.id)).scalars())
    shared = [a for a in (s.get(Account, i) for i in shared_ids) if a is not None and a not in own]
    usable = [a for a in own + shared if a.broker_kind != "021" or may_operate_021(s, a, user)]
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
        retire_stale_operator_rows(s)
        acct = s.get(Account, ucc)
        if acct is None:
            acct = Account(id=ucc, user_id=user.id, label="021 sandbox account", segments=["EQ", "FO"], trading_enabled=True, broker_kind="021", linked_via="operator")
            s.add(acct)
            s.flush()
            audit.record(s, "system", "ACCOUNT_LINKED_021", "user", user.id, {"account": mask(ucc), "role": "owner", "model": "CS_021_OWNER_EMAILS"})
        elif acct.broker_kind != "021":
            return None
        elif acct.linked_via != "operator":
            acct.linked_via = "operator"  # the gateway serves this UCC from .env, so the owner list now applies to it
        if acct.user_id != user.id:
            anchor = s.get(User, acct.user_id)
            if anchor is None or not (is_owner(anchor) or self_linked(s, ucc, anchor.id)):
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


def account_view(s: Session, a: Account, active_id: str | None, user_id: str | None = None) -> dict:
    linked_by = None
    if a.broker_kind == "021":
        linked_by = "self" if (user_id and self_linked(s, a.id, user_id)) else "operator"
    return {
        "account_id": a.id,
        "kind": a.broker_kind,
        "label": f"021 sandbox {mask(a.id)}" if a.broker_kind == "021" else a.label,
        "masked_id": mask(a.id) if a.broker_kind == "021" else a.id,
        "active": a.id == active_id,
        "status": "ready" if a.trading_enabled else "disabled",
        "linked_by": linked_by,
    }


# ------------------------------------------------------------------ self-service linking (API side; never a credential)


class LinkRefused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def gateway_http():
    """HTTP client to gateway021 over localhost (tests replace it with the gateway app's TestClient)."""
    import httpx

    return httpx.Client(base_url=settings().gateway021_url, timeout=30.0)


def _gateway_call(method: str, path: str, key: str, json_body: dict | None = None) -> tuple[int, dict]:
    import httpx

    try:
        with gateway_http() as c:
            r = c.request(method, path, json=json_body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        raise LinkRefused(503, "GATEWAY021_UNAVAILABLE", "The 021 gateway is not reachable right now. Nothing was stored.") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def gateway_link_key() -> dict:
    status, body = _gateway_call("GET", "/v1/link-key", settings().broker_read_key)
    if status != 200 or not body.get("public_key"):
        raise LinkRefused(503, "GATEWAY021_UNAVAILABLE", "The 021 gateway did not provide its link key. Try again shortly.")
    return body


def gateway_link(ciphertext: str, nonce: str, user_id: str) -> dict:
    """Relays the browser's ciphertext (opaque here) with the nonce this API issued and the caller's user id. The
    gateway answers with a PENDING link that confirm_link must confirm."""
    status, body = _gateway_call("POST", "/admin/link", settings().broker_admin_key, {"ciphertext": ciphertext, "nonce": nonce, "user_id": user_id})
    if status != 200:
        raise LinkRefused(status if status in (400, 401, 403, 409, 422, 429, 503) else 502, str(body.get("code") or "LINK_FAILED"), str(body.get("message") or "The 021 gateway refused the link."))
    return body


def gateway_confirm(account_id: str, user_id: str) -> dict:
    """Tells the gateway the account is attached, so it keeps the verified password (it deletes unconfirmed ones)."""
    status, body = _gateway_call("POST", "/admin/link/confirm", settings().broker_admin_key, {"account_id": account_id, "user_id": user_id})
    if status != 200:
        raise LinkRefused(409 if status == 409 else 503, str(body.get("code") or "LINK_CONFIRM_FAILED"), str(body.get("message") or "The 021 gateway could not complete the connection. Nothing was stored. Try again."))
    return body


def gateway_unlink(account_id: str, user_id: str, pending_only: bool = False) -> dict:
    status, body = _gateway_call("POST", "/admin/unlink", settings().broker_admin_key, {"account_id": account_id, "user_id": user_id, "pending_only": pending_only})
    if status != 200:
        raise LinkRefused(503 if status >= 500 else status, str(body.get("code") or "UNLINK_FAILED"), str(body.get("message") or "The 021 gateway could not delete the stored credential."))
    return body


def gateway_status_key(account_id: str) -> str | None:
    """The gateway's name for this account in its status heartbeat (an HMAC with a gateway-only key), or None when the
    gateway does not serve it or cannot be reached."""
    import httpx

    try:
        with gateway_http() as c:
            r = c.get("/v1/account-key", headers={"Authorization": f"Bearer {settings().broker_read_key}", "X-Account": account_id}, timeout=3.0)
        key = r.json().get("key") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return str(key) if key else None


def attach_self_link(s: Session, user: User, ucc: str) -> Account:
    """Records a verified self-service link and makes the account the trader's active one. Refuses a UCC that is
    already linked to another Countersign user (only the gateway's current .env account is excepted: proving its
    password links the trader as a member) and an id that belongs to a paper account. The account row is created
    race-safely (INSERT ... ON CONFLICT DO NOTHING, then SELECT ... FOR UPDATE), so two traders linking the same new UCC
    at once get one link and one clean refusal."""
    ucc = norm_ucc(ucc)
    env = is_env_account(ucc)
    created = s.execute(
        pg_insert(Account)
        .values(id=ucc, user_id=user.id, label="021 account", segments=["EQ", "FO"], trading_enabled=True, broker_kind="021", linked_via="operator" if env else "self")
        .on_conflict_do_nothing(index_elements=[Account.id])
        .returning(Account.id)
    ).first() is not None
    acct = s.execute(select(Account).where(Account.id == ucc).with_for_update().execution_options(populate_existing=True)).scalar_one()
    if acct.broker_kind != "021":
        raise LinkRefused(409, "UCC_CONFLICT", "That UCC cannot be linked here.")
    others = [x for x in s.execute(select(BrokerLink.user_id).where(BrokerLink.account_id == ucc)).scalars() if x != user.id]
    if not env and acct.linked_via != "self":
        acct.linked_via = "self"  # not the current .env account: the owner list never applies to it
    if not created and others and not env:
        raise LinkRefused(409, "UCC_LINKED_ELSEWHERE", "That 021 account is already connected to another Countersign user. Nothing was changed.")
    if not created and acct.user_id != user.id:
        anchor = s.get(User, acct.user_id)
        if acct.linked_via == "self" and not (anchor is not None and self_linked(s, ucc, anchor.id)):
            # A self-linked account whose earlier trader disconnected: the new trader becomes its anchor.
            acct.user_id = user.id
            stale = s.get(AccountMember, (ucc, user.id))
            if stale is not None:
                s.delete(stale)
        elif s.get(AccountMember, (ucc, user.id)) is None:
            s.add(AccountMember(account_id=ucc, user_id=user.id))
        s.flush()
    if s.get(BrokerLink, (ucc, user.id)) is None:
        s.add(BrokerLink(account_id=ucc, user_id=user.id))
        s.flush()
    user.active_account_id = ucc
    audit.record(s, "trader", "ACCOUNT_LINKED_021", "user", user.id, {"account": mask(ucc), "model": "self-service", "account_link": acct.linked_via})
    return acct


def detach_self_link(s: Session, user: User, account_id: str) -> None:
    """Removes the trader's own link (their grant) and, when it was active, moves them back to their paper account."""
    acct = s.get(Account, account_id)
    link = s.get(BrokerLink, (account_id, user.id)) if acct is not None else None
    if link is None:
        if acct is not None and acct.broker_kind == "021" and can_operate(s, user.id, account_id):
            raise LinkRefused(409, "LINK_OPERATOR_MANAGED", "This 021 account is linked by the operator (CS_021_OWNER_EMAILS), not by you, so it cannot be disconnected here.")
        raise LinkRefused(404, "ACCOUNT_NOT_FOUND", "You have no connected 021 account with that id.")
    s.delete(link)
    member = s.get(AccountMember, (account_id, user.id))
    if member is not None:
        s.delete(member)
    s.flush()
    if user.active_account_id == account_id or not can_operate(s, user.id, user.active_account_id or ""):
        paper = next((a for a in user_accounts(s, user) if a.broker_kind == "paper"), None)
        user.active_account_id = paper.id if paper else None
    audit.record(s, "trader", "ACCOUNT_UNLINKED_021", "user", user.id, {"account": mask(account_id), "model": "self-service"})
