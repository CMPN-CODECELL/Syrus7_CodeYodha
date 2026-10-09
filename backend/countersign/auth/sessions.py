"""Session cookies + CSRF. A logged-in session is NOT an approval: every trade still needs a fresh
countersignature over the exact canonical order."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import select

from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import SessionRow, User

COOKIE = "cs_session"
CSRF_COOKIE = "cs_csrf"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Principal:
    user_id: str
    username: str
    display_name: str
    session_id: str
    account_id: str


def create(user: User, user_agent: str) -> tuple[str, str, SessionRow]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    with session_scope() as s:
        row = SessionRow(
            token_hash=_h(token),
            csrf_token=csrf,
            user_id=user.id,
            user_agent=user_agent[:300],
            expires_at=datetime.now(UTC) + timedelta(minutes=settings().session_ttl_minutes),
        )
        s.add(row)
        s.flush()
        return token, csrf, row


def resolve(request: Request, *, check_csrf: bool = True) -> Principal:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, {"code": "NOT_SIGNED_IN", "message": "Please sign in to continue."})
    with session_scope() as s:
        row = s.execute(select(SessionRow).where(SessionRow.token_hash == _h(token))).scalar_one_or_none()
        if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
            raise HTTPException(401, {"code": "SESSION_EXPIRED", "message": "Your session expired or was revoked. Please sign in again."})
        if check_csrf and request.method in UNSAFE:
            header = request.headers.get("x-csrf-token", "")
            cookie = request.cookies.get(CSRF_COOKIE, "")
            if not header or not secrets.compare_digest(header, row.csrf_token) or not secrets.compare_digest(cookie, row.csrf_token):
                raise HTTPException(403, {"code": "CSRF_FAILED", "message": "Request blocked: missing or invalid CSRF token."})
        origin = request.headers.get("origin")
        if request.method in UNSAFE and origin and origin not in settings().allowed_origins:
            raise HTTPException(403, {"code": "ORIGIN_REJECTED", "message": f"Origin {origin} is not allowed."})
        user = s.get(User, row.user_id)
        from countersign.broker021.linking import active_account

        acct = active_account(s, user)  # the trader's chosen account (paper or the linked 021 account), never a shared fallback
        if acct is None:
            # Never fall back to a shared account: a user without their own paper account cannot act at all.
            raise HTTPException(403, {"code": "NO_ACCOUNT", "message": "No trading account is linked to this user."})
        return Principal(user_id=user.id, username=user.username, display_name=user.display_name, session_id=row.id, account_id=acct.id)


def current(request: Request) -> Principal:
    return resolve(request)


def check_origin(request: Request) -> None:
    """Login CSRF guard for the unauthenticated POSTs (signup, login, passkey sign-in): a browser always sends
    Origin on a POST, so a cross-site form or script is refused. Non-browser clients send none and are allowed."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings().allowed_origins:
        raise HTTPException(403, {"code": "ORIGIN_REJECTED", "message": f"Origin {origin} is not allowed."})
    if origin is None and request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, {"code": "ORIGIN_REJECTED", "message": "Cross-site sign-in requests are not allowed."})


def revoke_presented(request: Request) -> None:
    """Revokes the session cookie the browser presented (if any) before a new sign-in replaces it."""
    token = request.cookies.get(COOKIE)
    if not token:
        return
    with session_scope() as s:
        row = s.execute(select(SessionRow).where(SessionRow.token_hash == _h(token))).scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
