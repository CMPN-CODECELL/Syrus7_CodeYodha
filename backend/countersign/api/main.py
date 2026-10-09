"""Countersign API (FastAPI). Holds only the broker READ key. It can draft, verify and collect countersignatures,
but it has no code path and no credential that can place, modify or cancel an order on the trader's account."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from countersign.auth import countersign as cs
from countersign.auth import factors
from countersign.auth.sessions import COOKIE, CSRF_COOKIE, Principal, check_origin, create, current, revoke_presented
from countersign.core import audit, events, instruments, market, portfolio, state
from countersign.core.approval_crypto import CountersignError, b64url
from countersign.core.broker_client import BrokerAdmin, BrokerError, BrokerTimeout, BrokerUnavailable, ReadGateway, read_gateway
from countersign.core.config import rp_id_for, rp_id_from_credential, settings
from countersign.core.db import session_scope
from countersign.core.models import (
    Account,
    Approval,
    AttackRunRow,
    BrokerOrderMirror,
    Capability,
    ChatMessage,
    DeviceCredential,
    ExecJob,
    OrderFill,
    OutboxEntry,
    OutcomeRow,
    Plan,
    PlanStep,
    Proposal,
    SecurityEvent,
    SessionRow,
    StandingMandate,
    User,
    WebauthnChallenge,
)
from countersign.core.proposals import mandate_view, view
from countersign.core.schemas import iso

app = FastAPI(title="Countersign API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

import collections
import uuid as _uuid

_RATE: dict[str, collections.deque] = collections.defaultdict(collections.deque)
# (path, method, limit, window seconds, keyed by client IP). A path ending in "/" matches as a fragment (the approval
# endpoints carry a proposal id); every other path must match exactly, and only the first matching rule applies.
# Sign-in endpoints are always keyed by IP so rotating cookies cannot dodge them; signed-in endpoints by session
# cookie (IP when there is none).
RATE_LIMITS = [
    ("/approval/", "POST", 12, 60.0, False),
    ("/api/chat", "POST", 20, 60.0, False),
    ("/api/auth/pin", "POST", 10, 60.0, False),
    ("/api/auth/pin/reset", "POST", 5, 60.0, False),
    ("/api/auth/step-up", "POST", 10, 60.0, False),
    ("/api/auth/step-up/start", "POST", 10, 60.0, False),
    ("/api/attack-lab/run", "POST", 6, 60.0, False),
    ("/api/auth/signup", "POST", 5, 60.0, True),
    ("/api/auth/login", "POST", 10, 60.0, True),
    ("/api/auth/passkey/login/start", "POST", 10, 60.0, True),
    ("/api/auth/passkey/login/finish", "POST", 10, 60.0, True),
    ("/api/public/status", "GET", 60, 60.0, True),
]
_LOOPBACK = {"127.0.0.1", "::1"}


def client_ip(request: Request) -> str:
    """The TCP peer (uvicorn runs with proxy_headers=False). X-Forwarded-For is never trusted: the API listens on
    loopback only, and the Next.js proxy passes a client-supplied X-Forwarded-For through unchanged (it neither sets
    nor appends one for rewrites), so any value in it is the client's choice. Every browser behind the local proxy
    therefore shares the loopback bucket."""
    return request.client.host if request.client else "anon"


def _rate_rule(method: str, path: str) -> tuple[str, int, float, bool] | None:
    for key, m, limit, window, by_ip in RATE_LIMITS:
        if m == method and (path == key or (key.endswith("/") and key in path)):
            return key, limit, window, by_ip
    return None


def _over_limit(bucket: str, limit: int, window: float, now: float) -> int:
    """0 when the request may proceed (and is counted), otherwise the seconds until the window has room again."""
    q = _RATE[bucket]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return max(1, int(window - (now - q[0])) + 1)
    q.append(now)
    return 0


@app.middleware("http")
async def _hardening(request: Request, call_next):
    rid = request.headers.get("x-request-id") or _uuid.uuid4().hex[:16]
    rule = _rate_rule(request.method, request.url.path)
    if rule:
        key, limit, window, by_ip = rule
        who = "ip:" + client_ip(request) if by_ip else request.cookies.get(COOKIE, client_ip(request))[:24]
        wait = _over_limit(who + key, limit, window, time.time())
        if wait:
            return JSONResponse(
                {"code": "RATE_LIMITED", "message": f"Too many attempts. Wait {wait} s and try again.", "retry_after_s": wait},
                status_code=429,
                headers={"X-Request-ID": rid, "Retry-After": str(wait)},
            )
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


ERROR_STATUS = {
    "NOT_FOUND": 404,
    "APPROVAL_EXPIRED": 409,
    "PRICE_DRIFT": 409,
    "STALE_MARKET_DATA": 409,
    "ALREADY_APPROVED": 409,
    "PROPOSAL_BLOCKED": 422,
    "SIGNATURE_INVALID": 401,
    "PAYLOAD_MISMATCH": 409,
    "PIN_INVALID": 401,
    "PIN_LOCKED": 423,
    "TOTP_INVALID": 401,
    "TOTP_REPLAY": 401,
    "KILL_SWITCH_ENGAGED": 423,
    "NO_DEVICE_REGISTERED": 412,
    "CHALLENGE_EXPIRED": 410,
}


@app.exception_handler(CountersignError)
def _cs_err(_: Request, e: CountersignError):
    return JSONResponse({"code": e.code, "message": e.message}, status_code=ERROR_STATUS.get(e.code, 422))


@app.exception_handler(HTTPException)
def _http_err(_: Request, e: HTTPException):
    d = e.detail if isinstance(e.detail, dict) else {"code": "ERROR", "message": str(e.detail)}
    return JSONResponse(d, status_code=e.status_code, headers=e.headers)


@app.exception_handler(RequestValidationError)
def _validation_err(_: Request, e: RequestValidationError):
    first = (e.errors() or [{}])[0]
    field = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path")) or "request"
    return JSONResponse({"code": "INVALID_INPUT", "field": field, "message": f"Invalid {field}: {first.get('msg', 'invalid value')}."}, status_code=422)


@app.exception_handler(BrokerUnavailable)
def _broker_down(_: Request, e: BrokerUnavailable):
    return JSONResponse({"code": "BROKER_UNAVAILABLE", "message": "The broker (paper broker) is not reachable. Account data is temporarily unavailable."}, status_code=503)


@app.exception_handler(BrokerError)
def _broker_err(_: Request, e: BrokerError):
    return JSONResponse({"code": e.code, "message": e.message}, status_code=502)


@app.on_event("startup")
def _startup() -> None:
    cfg = settings()
    if cfg.broker_write_key:
        sys.exit("api: refusing to start. The API process must never hold the broker WRITE key (credential zone violation).")
    from countersign.core.bootstrap import bootstrap
    from countersign.core.mandate_engine import start_background

    bootstrap()
    start_background()


_gw: ReadGateway | None = None


def gw() -> ReadGateway:
    """Health probes only. Account data always goes through read_gateway(<the caller's own account>)."""
    global _gw
    if _gw is None:
        _gw = ReadGateway()
    return _gw


def broker_health() -> tuple[bool, int | None]:
    return gw().health()


def _user(s, p: Principal) -> User:
    return s.get(User, p.user_id)


# ------------------------------------------------------------------ auth


class DemoLogin(BaseModel):
    username: str | None = None


def _me(s, p_user: User, session_row: SessionRow) -> dict:
    devices = s.execute(select(DeviceCredential).where(DeviceCredential.user_id == p_user.id).order_by(DeviceCredential.created_at)).scalars().all()
    acct = s.execute(select(Account).where(Account.user_id == p_user.id).order_by(Account.id)).scalars().first()
    pin_set = bool(p_user.pin_hash)
    passkeys = sum(1 for d in devices if d.revoked_at is None and not d.is_virtual)
    return {
        "user": {"id": p_user.id, "username": p_user.username, "display_name": p_user.display_name},
        "email": p_user.email,
        "account_id": acct.id if acct else None,
        "account": {"id": acct.id, "label": acct.label} if acct else None,
        "devices": [_device(d) for d in devices],
        "has_pin": pin_set,
        "has_totp": bool(p_user.totp_confirmed),
        "pin_locked_until": iso(p_user.pin_locked_until),
        "session": {"id": session_row.id, "created_at": iso(session_row.created_at), "expires_at": iso(session_row.expires_at)},
        # Onboarding is complete once the trader chose their own PIN; passkey and authenticator are encouraged.
        "onboarding": {"pin_set": pin_set, "passkeys": passkeys, "totp": bool(p_user.totp_confirmed), "complete": pin_set},
        "demo_mode": settings().demo_mode,
    }


def _device(d: DeviceCredential) -> dict:
    return {"id": d.id, "label": d.label, "created_at": iso(d.created_at), "last_used_at": iso(d.last_used_at), "revoked": d.revoked_at is not None, "is_virtual": d.is_virtual, "transports": d.transports or []}


def _signed_in(request: Request, user_id: str, method: str, status_code: int = 200) -> JSONResponse:
    """Mints a brand-new session (never reuses the presented one: no session fixation) and returns Me + cookies."""
    revoke_presented(request)
    with session_scope() as s:
        u = s.get(User, user_id)
    token, csrf, row = create(u, request.headers.get("user-agent", ""))
    with session_scope() as s:
        u = s.get(User, user_id)
        audit.record(s, "trader", "SESSION_CREATED", "session", row.id, {"method": method, "note": "Login is not approval; each trade needs a fresh countersignature."})
        body_out = _me(s, u, s.get(SessionRow, row.id))
    resp = JSONResponse(body_out, status_code=status_code)
    secure = request.url.scheme == "https"
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=secure, max_age=settings().session_ttl_minutes * 60, path="/")
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", secure=secure, max_age=settings().session_ttl_minutes * 60, path="/")
    return resp


def _automation_only(request: Request) -> None:
    """Demo sign-in is for scripts on this machine that call the API directly. A request that came through the web
    proxy (which always adds X-Forwarded-Host), from a foreign origin or from another host is refused, so the web
    server can never hand a credential-free session to someone on the network."""
    check_origin(request)
    peer = request.client.host if request.client else ""
    if peer not in _LOOPBACK or any(h in request.headers for h in ("x-forwarded-for", "x-forwarded-host")):
        raise HTTPException(403, {"code": "FORBIDDEN", "message": "Demo sign-in is only for automation scripts on this machine. Sign in with your account."})


@app.post("/api/auth/demo-login")
def demo_login(body: DemoLogin, request: Request):
    """Automation only (smoke/flow scripts, tests), demo mode only. The web UI never calls this."""
    _demo_only()
    _automation_only(request)
    username = body.username or "demo"
    with session_scope() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if u is None or u.email is not None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such demo user."})
        if u.is_lab_user:
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "The Attack Lab identity cannot sign in."})
        uid = u.id
    return _signed_in(request, uid, "demo")


# ------------------------------------------------------------------ accounts: sign-up, sign-in, passkey sign-in

EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}")
INVALID_CREDENTIALS = {"code": "INVALID_CREDENTIALS", "message": "Email or password is incorrect."}
LOGIN_FREE_ATTEMPTS = 5
LOGIN_LOCK_BASE_S = 30
LOGIN_LOCK_MAX_S = 15 * 60
LOGIN_WINDOW_S = 15 * 60  # a failure more than this after the previous one starts the count again
ACCOUNT_BACKSTOP_FAILURES = 50  # account-wide lock, reachable only from many clients (one client is held to ~11 per window)


def _invalid(field: str, message: str) -> HTTPException:
    return HTTPException(422, {"code": "INVALID_INPUT", "field": field, "message": message})


def _lock_seconds(failures: int) -> int:
    """No lock for the first 4 misses; the 5th locks for 30 s, then 60, 120 ... capped at 15 minutes."""
    if failures < LOGIN_FREE_ATTEMPTS:
        return 0
    return min(LOGIN_LOCK_BASE_S * 2 ** (failures - LOGIN_FREE_ATTEMPTS), LOGIN_LOCK_MAX_S)


def _locked(retry_after_s: int) -> HTTPException:
    secs = max(1, int(retry_after_s))
    return HTTPException(
        429,
        {"code": "LOGIN_LOCKED", "message": f"Too many failed sign-in attempts. Try again in {secs} s, or sign in with your passkey.", "retry_after_s": secs},
        headers={"Retry-After": str(secs)},
    )


# Failed password sign-ins per (email, client): (failures, locked until, last failure). The lock is keyed on the
# pair, so a stranger who knows an email locks out only their own client, never the owner elsewhere, and passkey
# sign-in works throughout (and clears it). Unknown emails take the same path, so the 401 and 429 sequence and the
# Argon2 cost never reveal whether an email is registered (sign-up does reveal it, by design).
_FAILS: dict[tuple[str, str], tuple[int, float, float]] = {}
_FAILS_LOCK = threading.Lock()


def _client_locked_for(key: tuple[str, str]) -> float:
    with _FAILS_LOCK:
        _, until, _ = _FAILS.get(key, (0, 0.0, 0.0))
    return max(0.0, until - time.time())


def _client_failure(key: tuple[str, str]) -> tuple[int, int]:
    """Counts a failure and returns (failures in the window, seconds locked now)."""
    now = time.time()
    with _FAILS_LOCK:
        if len(_FAILS) > 10_000:
            _FAILS.clear()
        n, _, last = _FAILS.get(key, (0, 0.0, 0.0))
        n = 1 if now - last > LOGIN_WINDOW_S else n + 1
        wait = _lock_seconds(n)
        _FAILS[key] = (n, now + wait if wait else 0.0, now)
    return n, wait


def _clear_failures(email: str, client: str | None = None) -> None:
    with _FAILS_LOCK:
        for k in [k for k in _FAILS if k[0] == email and (client is None or k[1] == client)]:
            del _FAILS[k]


def _username_for(email: str) -> str:
    return email if len(email) <= 64 else f"{email[:48]}~{secrets.token_hex(4)}"


def _new_paper_account(s) -> str:
    """Allocates PT + 6 base32 characters and provisions it at the broker, seeded like DEMO1."""
    admin = BrokerAdmin()
    for _ in range(8):
        acct_id = "PT" + base64.b32encode(secrets.token_bytes(5)).decode()[:6]
        if s.get(Account, acct_id) is not None:
            continue
        try:
            admin.create_account(acct_id)
            return acct_id
        except BrokerError as e:
            if e.code == "ACCOUNT_EXISTS":
                continue
            raise
    raise HTTPException(503, {"code": "PROVISIONING_FAILED", "message": "Could not allocate a paper trading account. Try again."})


class SignupBody(BaseModel):
    display_name: str = ""
    email: str = ""
    password: str = ""


@app.post("/api/auth/signup")
def signup(body: SignupBody, request: Request):
    check_origin(request)
    name = body.display_name.strip()
    email = body.email.strip().lower()
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise _invalid("display_name", "Display name must be 1 to 64 printable characters.")
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise _invalid("email", "Enter a valid email address.")
    if not 8 <= len(body.password) <= 128:
        raise _invalid("password", "Password must be 8 to 128 characters.")
    if body.password.strip().lower() == email:
        raise _invalid("password", "Your password must not be your email address.")
    taken = HTTPException(409, {"code": "EMAIL_TAKEN", "message": "An account with this email already exists. Sign in instead."})
    with session_scope() as s:
        if s.execute(select(User.id).where(func.lower(User.email) == email)).first() is not None:
            raise taken
    pw_hash = factors.hash_password(body.password)
    try:
        with session_scope() as s:
            u = User(username=_username_for(email), display_name=name, email=email, password_hash=pw_hash, webauthn_user_handle=secrets.token_bytes(32))
            s.add(u)
            s.flush()  # the unique email index is checked here, before any broker call
            acct_id = _new_paper_account(s)
            s.add(Account(id=acct_id, user_id=u.id, label="Paper trading account", segments=["EQ", "FO"], trading_enabled=True))
            audit.record(s, "trader", "ACCOUNT_CREATED", "user", u.id, {"account_id": acct_id, "paper": True})
            uid = u.id
    except IntegrityError:
        raise taken from None
    return _signed_in(request, uid, "signup", status_code=201)


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request):
    check_origin(request)
    email = body.email.strip().lower()
    client = client_ip(request)
    key = (email, client)
    now = datetime.now(UTC)
    with session_scope() as s:
        u = s.execute(select(User).where(func.lower(User.email) == email).with_for_update()).scalar_one_or_none() if email else None
        real = u is not None and u.password_hash is not None and not u.is_lab_user
        wait_s = _client_locked_for(key)
        if real and u.login_locked_until and u.login_locked_until > now:
            wait_s = max(wait_s, (u.login_locked_until - now).total_seconds())
        if wait_s > 0:
            factors.check_password(None, body.password)  # a locked answer costs the same Argon2 time as any other
            raise _locked(wait_s)
        if not factors.check_password(u.password_hash if real else None, body.password):
            n, wait = _client_failure(key)
            if real:
                # Account-wide backstop with its own decay: only a distributed attack (many clients) can reach it.
                stale = u.login_last_failed_at is None or (now - u.login_last_failed_at).total_seconds() > LOGIN_WINDOW_S
                u.login_failed_count = 1 if stale else (u.login_failed_count or 0) + 1
                u.login_last_failed_at = now
                if u.login_failed_count >= ACCOUNT_BACKSTOP_FAILURES:
                    u.login_locked_until = now + timedelta(seconds=LOGIN_LOCK_MAX_S)
                    wait = max(wait, LOGIN_LOCK_MAX_S)
                data = {"failures": n, "account_failures": u.login_failed_count}
                audit.record(s, "trader", "LOGIN_LOCKED" if wait else "LOGIN_FAILED", "user", u.id, {**data, "locked_s": wait} if wait else data)
                s.commit()
            raise _locked(wait) if wait else HTTPException(401, INVALID_CREDENTIALS)
        _clear_failures(email, client)
        u.login_failed_count = 0
        u.login_locked_until = None
        u.login_last_failed_at = None
        uid = u.id
    return _signed_in(request, uid, "password")


@app.post("/api/auth/passkey/login/start")
def passkey_login_start(request: Request):
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import UserVerificationRequirement

    check_origin(request)
    opts = generate_authentication_options(rp_id=rp_id_for(request.headers.get("origin")), allow_credentials=[], user_verification=UserVerificationRequirement.REQUIRED, timeout=120000)
    now = datetime.now(UTC)
    with session_scope() as s:
        s.execute(delete(WebauthnChallenge).where(WebauthnChallenge.purpose == "LOGIN", WebauthnChallenge.expires_at < now - timedelta(hours=1)))
        ch = WebauthnChallenge(user_id=None, purpose="LOGIN", challenge=opts.challenge, expires_at=now + timedelta(minutes=3))
        s.add(ch)
        s.flush()
        return {"challenge_id": ch.id, "options": json.loads(options_to_json(opts))}


class PasskeyFinishBody(BaseModel):
    challenge_id: str = ""
    credential: dict = {}


@app.post("/api/auth/passkey/login/finish")
def passkey_login_finish(body: PasskeyFinishBody, request: Request):
    """Signs in with a discoverable passkey. This is NOT a trade approval: every trade still needs a fresh
    countersignature over the exact order."""
    from webauthn import verify_authentication_response

    check_origin(request)
    now = datetime.now(UTC)
    with session_scope() as s:
        ch = s.get(WebauthnChallenge, body.challenge_id, with_for_update=True) if body.challenge_id else None
        if ch is None or ch.purpose != "LOGIN" or ch.user_id is not None or ch.used_at is not None or ch.expires_at <= now:
            raise HTTPException(410, {"code": "CHALLENGE_EXPIRED", "message": "This passkey sign-in request expired or was already used. Start again."})
        ch.used_at = now  # single use, even if the assertion below fails
        challenge = ch.challenge
    not_recognized = HTTPException(401, {"code": "PASSKEY_NOT_RECOGNIZED", "message": "This passkey is not registered to a Countersign account (or was revoked). Sign in with your email and password."})
    cred = body.credential or {}
    cred_id = cred.get("id") or cred.get("rawId")
    with session_scope() as s:
        d = s.execute(select(DeviceCredential).where(DeviceCredential.credential_id == cred_id)).scalar_one_or_none() if isinstance(cred_id, str) and cred_id else None
        u = s.get(User, d.user_id) if d is not None else None
        if d is None or u is None or d.revoked_at is not None or d.is_virtual or u.is_lab_user:
            raise not_recognized
        resp_part = cred.get("response") if isinstance(cred.get("response"), dict) else {}
        handle = resp_part.get("userHandle")
        if handle and str(handle).rstrip("=") != b64url(u.webauthn_user_handle):
            raise not_recognized
        try:
            v = verify_authentication_response(
                credential=cred,
                expected_challenge=challenge,
                expected_rp_id=rp_id_from_credential(cred),
                expected_origin=settings().allowed_origins,
                credential_public_key=d.public_key,
                credential_current_sign_count=d.sign_count,
                require_user_verification=True,
            )
        except Exception as e:  # noqa: BLE001
            audit.record(s, "trader", "PASSKEY_LOGIN_REJECTED", "device", d.id, {"reason": type(e).__name__})
            s.commit()
            raise HTTPException(401, {"code": "SIGNATURE_INVALID", "message": "The passkey signature could not be verified. Try again."}) from None
        d.sign_count = v.new_sign_count
        d.last_used_at = now
        # The owner proved possession of a passkey: any password lockout (from any client) is lifted.
        if u.email:
            _clear_failures(u.email)
        u.login_failed_count = 0
        u.login_locked_until = None
        u.login_last_failed_at = None
        uid = u.id
    return _signed_in(request, uid, "passkey")


@app.post("/api/auth/logout")
def logout(p: Principal = Depends(current)):
    with session_scope() as s:
        row = s.get(SessionRow, p.session_id)
        row.revoked_at = datetime.now(UTC)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(p: Principal = Depends(current)):
    with session_scope() as s:
        return _me(s, _user(s, p), s.get(SessionRow, p.session_id))


# ------------------------------------------------------------------ step-up: fresh proof before changing approval methods

STEP_UP_TTL = timedelta(minutes=10)


def _active_passkeys(s, user_id: str) -> list[DeviceCredential]:
    return list(s.execute(select(DeviceCredential).where(DeviceCredential.user_id == user_id, DeviceCredential.revoked_at.is_(None), DeviceCredential.is_virtual.is_(False))).scalars())


def _factor_methods(s, u: User) -> list[str]:
    return [m for m, ok in (("webauthn", bool(_active_passkeys(s, u.id))), ("pin", bool(u.pin_hash)), ("totp", bool(u.totp_confirmed))) if ok]


def _step_up_fresh(s, p: Principal, methods: tuple[str, ...] = ("pin", "totp", "webauthn")) -> str | None:
    """The method this session last proved within the step-up window, if it is one of `methods`."""
    row = s.get(SessionRow, p.session_id)
    if row and row.step_up_at and row.step_up_method in methods and datetime.now(UTC) - row.step_up_at < STEP_UP_TTL:
        return row.step_up_method
    return None


def _grant_step_up(s, p: Principal, method: str) -> datetime:
    row = s.get(SessionRow, p.session_id)
    row.step_up_at = datetime.now(UTC)
    row.step_up_method = method
    return row.step_up_at + STEP_UP_TTL


def _require_step_up(s, u: User, p: Principal) -> None:
    """Once a trader has any approval method, adding or replacing one needs a fresh proof with an existing one, so a
    password or a stolen session alone can never mint a new way to countersign. Onboarding (no method yet) is open."""
    methods = _factor_methods(s, u)
    if methods and not _step_up_fresh(s, p):
        raise HTTPException(
            403,
            {
                "code": "STEP_UP_REQUIRED",
                "message": "Confirm it is you first with your passkey, app PIN or authenticator code. Being signed in is not enough to add or replace a way to countersign.",
                "methods": methods,
            },
        )


def _factor_changed(s, p: Principal, kind: str, message: str) -> None:
    """Approval-method changes are audited and listed in the trader's own Security Center."""
    state.security_event(s, "info", kind, message, {"account_id": p.account_id, "user_id": p.user_id}, actor="trader")


class StepUpBody(BaseModel):
    method: str = ""
    pin: str | None = None
    code: str | None = None
    challenge_id: str | None = None
    credential: dict | None = None


@app.post("/api/auth/step-up/start")
def step_up_start(request: Request, p: Principal = Depends(current)):
    """Passkey options to re-confirm with one of this trader's registered passkeys."""
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

    from countersign.core.approval_crypto import b64url_decode

    now = datetime.now(UTC)
    with session_scope() as s:
        creds = _active_passkeys(s, p.user_id)
        if not creds:
            raise CountersignError("NO_DEVICE_REGISTERED", "No passkey is registered. Confirm with your app PIN or authenticator code instead.")
        opts = generate_authentication_options(
            rp_id=rp_id_for(request.headers.get("origin")),
            allow_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(c.credential_id)) for c in creds],
            user_verification=UserVerificationRequirement.REQUIRED,
            timeout=120000,
        )
        ch = WebauthnChallenge(user_id=p.user_id, purpose="STEP_UP", challenge=opts.challenge, expires_at=now + timedelta(minutes=3))
        s.add(ch)
        s.flush()
        return {"challenge_id": ch.id, "options": json.loads(options_to_json(opts))}


def _verify_step_up_passkey(s, u: User, body: StepUpBody) -> None:
    from webauthn import verify_authentication_response

    now = datetime.now(UTC)
    ch = s.get(WebauthnChallenge, body.challenge_id, with_for_update=True) if body.challenge_id else None
    if ch is None or ch.user_id != u.id or ch.purpose != "STEP_UP" or ch.used_at is not None or ch.expires_at <= now:
        raise CountersignError("CHALLENGE_EXPIRED", "This passkey request expired or was already used. Start again.")
    ch.used_at = now  # single use, even if the assertion below fails
    cred = body.credential or {}
    cid = cred.get("id") or cred.get("rawId")
    d = s.execute(select(DeviceCredential).where(DeviceCredential.credential_id == cid)).scalar_one_or_none() if isinstance(cid, str) and cid else None
    if d is None or d.user_id != u.id or d.revoked_at is not None or d.is_virtual:
        raise CountersignError("SIGNATURE_INVALID", "That passkey is not registered to your account (or was revoked).")
    try:
        v = verify_authentication_response(
            credential=cred,
            expected_challenge=ch.challenge,
            expected_rp_id=rp_id_from_credential(cred),
            expected_origin=settings().allowed_origins,
            credential_public_key=d.public_key,
            credential_current_sign_count=d.sign_count,
            require_user_verification=True,
        )
    except Exception:  # noqa: BLE001
        raise CountersignError("SIGNATURE_INVALID", "The passkey signature could not be verified. Try again.") from None
    d.sign_count = v.new_sign_count
    d.last_used_at = now


@app.post("/api/auth/step-up")
def step_up(body: StepUpBody, p: Principal = Depends(current)):
    """Fresh proof of an existing approval method for this session, valid 10 minutes. It allows changing approval
    methods (adding a passkey, linking or replacing the authenticator app). It is never a trade approval."""
    if body.method not in ("pin", "totp", "webauthn"):
        raise HTTPException(422, {"code": "METHOD_INVALID", "message": "Confirm with your passkey, app PIN or authenticator code."})
    with session_scope() as s:
        u = s.get(User, p.user_id, with_for_update=True)
        try:
            if body.method == "pin":
                factors.verify_pin(s, u, body.pin or "")
            elif body.method == "totp":
                factors.verify_totp(u, body.code or "")
            else:
                _verify_step_up_passkey(s, u, body)
        except CountersignError:
            audit.record(s, "trader", "STEP_UP_REJECTED", "user", u.id, {"method": body.method})
            s.commit()
            raise
        until = _grant_step_up(s, p, body.method)
        audit.record(s, "trader", "STEP_UP_VERIFIED", "user", u.id, {"method": body.method, "session_id": p.session_id})
        return {"ok": True, "method": body.method, "expires_at": iso(until)}


class RegStart(BaseModel):
    device_label: str | None = None


@app.post("/api/auth/webauthn/register/start")
def reg_start(body: RegStart, request: Request, p: Principal = Depends(current)):
    from datetime import timedelta

    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorAttachment,
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    from countersign.core.approval_crypto import b64url_decode

    with session_scope() as s:
        u = _user(s, p)
        _require_step_up(s, u, p)
        existing = s.execute(select(DeviceCredential).where(DeviceCredential.user_id == u.id, DeviceCredential.revoked_at.is_(None))).scalars().all()
        opts = generate_registration_options(
            rp_id=rp_id_for(request.headers.get("origin")),
            rp_name=settings().rp_name,
            user_name=u.email or u.username,
            user_id=u.webauthn_user_handle,
            user_display_name=u.display_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,  # discoverable, so it can also sign in
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(c.credential_id)) for c in existing],
            timeout=120000,
        )
        s.add(WebauthnChallenge(user_id=u.id, purpose=f"REGISTRATION:{(body.device_label or 'This device')[:60]}", challenge=opts.challenge, expires_at=datetime.now(UTC) + timedelta(minutes=3)))
        return json.loads(options_to_json(opts))


@app.post("/api/auth/webauthn/register/finish")
async def reg_finish(request: Request, p: Principal = Depends(current)):
    from webauthn import verify_registration_response

    cred = await request.json()
    with session_scope() as s:
        u = _user(s, p)
        _require_step_up(s, u, p)
        ch = s.execute(
            select(WebauthnChallenge).where(WebauthnChallenge.user_id == u.id, WebauthnChallenge.purpose.like("REGISTRATION%"), WebauthnChallenge.used_at.is_(None)).order_by(WebauthnChallenge.created_at.desc())
        ).scalars().first()
        if ch is None or ch.expires_at <= datetime.now(UTC):
            raise HTTPException(409, {"code": "REGISTRATION_EXPIRED", "message": "The passkey registration window expired. Start again."})
        try:
            v = verify_registration_response(credential=cred, expected_challenge=ch.challenge, expected_rp_id=rp_id_from_credential(cred), expected_origin=settings().allowed_origins, require_user_verification=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, {"code": "REGISTRATION_FAILED", "message": f"Passkey registration could not be verified: {e}"}) from e
        ch.used_at = datetime.now(UTC)
        label = ch.purpose.split(":", 1)[1] if ":" in ch.purpose else "This device"
        d = DeviceCredential(
            user_id=u.id,
            credential_id=b64url(v.credential_id),
            public_key=v.credential_public_key,
            sign_count=v.sign_count,
            transports=cred.get("response", {}).get("transports", []) or [],
            label=label,
            aaguid=str(v.aaguid),
            backed_up=bool(v.credential_backed_up),
        )
        s.add(d)
        s.flush()
        audit.record(s, "trader", "DEVICE_REGISTERED", "device", d.id, {"label": label, "aaguid": str(v.aaguid)})
        _factor_changed(s, p, "PASSKEY_REGISTERED", f"Passkey registered: {label}. It can now countersign your trades.")
        return _device(d)


class PinBody(BaseModel):
    pin: str
    current_pin: str | None = None


@app.post("/api/auth/pin")
def set_pin(body: PinBody, p: Principal = Depends(current)):
    with session_scope() as s:
        u = _user(s, p)
        first = not u.pin_hash
        had_method = bool(_factor_methods(s, u))
        try:
            factors.set_pin(s, u, body.pin, body.current_pin)
        except CountersignError:
            s.commit()
            raise
        audit.record(s, "trader", "PIN_SET", "user", u.id, {"changed": not first})
        # Choosing the very first approval method (onboarding), or proving the current PIN to change it, is a fresh
        # step-up, so onboarding goes straight on to the passkey and authenticator steps.
        if not had_method or not first:
            _grant_step_up(s, p, "pin")
        if not first:
            _factor_changed(s, p, "PIN_CHANGED", "Your app PIN was changed (current PIN verified).")
    return {"ok": True}


class PinResetBody(BaseModel):
    password: str = ""
    new_pin: str = ""
    totp: str | None = None


@app.post("/api/auth/pin/reset")
def pin_reset(body: PinResetBody, p: Principal = Depends(current)):
    """PIN recovery for a signed-in trader: the account password plus, when the trader has one, a second method that
    is not the PIN (a current authenticator code, or a passkey confirmed through /api/auth/step-up). Sets the new PIN,
    clears any PIN lockout and signs out every other session. A PIN-only account recovers with the password alone."""
    factors.check_pin_strength(body.new_pin)
    with session_scope() as s:
        u = s.get(User, p.user_id, with_for_update=True)
        if u.password_hash is None:
            raise HTTPException(401, {"code": "INVALID_CREDENTIALS", "message": "This account has no password, so its PIN cannot be recovered this way."})
        if not factors.check_password(u.password_hash, body.password):
            audit.record(s, "trader", "PIN_RESET_REJECTED", "user", u.id, {"reason": "password"})
            s.commit()
            raise HTTPException(401, {"code": "INVALID_CREDENTIALS", "message": "The account password is incorrect."})
        has_passkey = bool(_active_passkeys(s, u.id))
        second = None
        if u.totp_confirmed and (body.totp or "").strip():
            try:
                factors.verify_totp(u, body.totp or "")
            except CountersignError as e:
                raise HTTPException(422, {"code": "TOTP_INVALID", "message": e.message}) from None
            second = "authenticator"
        elif u.totp_confirmed or has_passkey:
            proved = _step_up_fresh(s, p, ("webauthn", "totp"))
            if proved:
                second = "passkey" if proved == "webauthn" else "authenticator"
            elif u.totp_confirmed:
                raise HTTPException(422, {"code": "TOTP_REQUIRED", "message": "Enter the 6-digit code from your authenticator app as well" + (", or confirm with your passkey." if has_passkey else ".")})
            else:
                raise HTTPException(422, {"code": "PASSKEY_REQUIRED", "message": "Confirm with your passkey as well. While a passkey is registered, the password alone cannot reset the PIN."})
        factors.reset_pin(u, body.new_pin)
        # A recovery event: every other session of this trader is signed out.
        revoked = s.execute(update(SessionRow).where(SessionRow.user_id == u.id, SessionRow.id != p.session_id, SessionRow.revoked_at.is_(None)).values(revoked_at=datetime.now(UTC))).rowcount
        audit.record(s, "trader", "PIN_RESET", "user", u.id, {"method": f"password+{second}" if second else "password", "other_sessions_revoked": revoked})
        with_second = f" and your {second}" if second else ""
        _factor_changed(s, p, "PIN_RESET", f"Your app PIN was reset with your password{with_second}. Other sessions were signed out.")
    return {"ok": True}


@app.post("/api/auth/totp/setup")
def totp_setup(p: Principal = Depends(current)):
    """Starts linking an authenticator app. The new secret stays pending (a linked one keeps working) until a code
    from it is confirmed. Once the trader has any approval method this needs a fresh step-up."""
    with session_scope() as s:
        u = _user(s, p)
        _require_step_up(s, u, p)
        uri, secret = factors.totp_setup(u)
        audit.record(s, "trader", "TOTP_SETUP_STARTED", "user", u.id, {"replacing": bool(u.totp_confirmed)})
        return {"otpauth_uri": uri, "secret": secret}


class CodeBody(BaseModel):
    code: str


@app.post("/api/auth/totp/verify")
def totp_verify(body: CodeBody, p: Principal = Depends(current)):
    with session_scope() as s:
        u = _user(s, p)
        replacing = bool(u.totp_confirmed)
        factors.verify_totp(u, body.code, confirm=True)
        audit.record(s, "trader", "TOTP_CONFIRMED", "user", u.id, {"replaced": replacing})
        if replacing:
            _factor_changed(s, p, "AUTHENTICATOR_REPLACED", "A new authenticator app replaced the previous one.")
        else:
            _factor_changed(s, p, "AUTHENTICATOR_LINKED", "An authenticator app was linked to your account.")
    return {"ok": True}


@app.delete("/api/auth/devices/{device_id}")
def revoke_device(device_id: str, p: Principal = Depends(current)):
    with session_scope() as s:
        d = s.get(DeviceCredential, device_id)
        if d is None or d.user_id != p.user_id:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such device."})
        d.revoked_at = datetime.now(UTC)
        audit.record(s, "trader", "DEVICE_REVOKED", "device", d.id, {"label": d.label})
    return {"ok": True}


@app.get("/api/sessions")
def sessions(p: Principal = Depends(current)):
    with session_scope() as s:
        rows = s.execute(select(SessionRow).where(SessionRow.user_id == p.user_id).order_by(SessionRow.created_at.desc()).limit(20)).scalars().all()
        return [{"id": r.id, "created_at": iso(r.created_at), "expires_at": iso(r.expires_at), "user_agent": r.user_agent, "current": r.id == p.session_id, "revoked": r.revoked_at is not None} for r in rows]


@app.delete("/api/sessions/{sid}")
def revoke_session(sid: str, p: Principal = Depends(current)):
    with session_scope() as s:
        r = s.get(SessionRow, sid)
        if r is None or r.user_id != p.user_id:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such session."})
        r.revoked_at = datetime.now(UTC)
        audit.record(s, "trader", "SESSION_REVOKED", "session", sid, {})
    return {"ok": True}


# ------------------------------------------------------------------ account & market (read-only)


@app.get("/api/account/summary")
def account_summary(p: Principal = Depends(current)):
    with session_scope() as s:
        return portfolio.summary(s, read_gateway(p.account_id), p.account_id)


@app.get("/api/account/holdings")
def account_holdings(p: Principal = Depends(current)):
    with session_scope() as s:
        return portfolio.holdings(s, read_gateway(p.account_id))


@app.get("/api/account/positions")
def account_positions(p: Principal = Depends(current)):
    with session_scope() as s:
        return portfolio.positions(s, read_gateway(p.account_id))


@app.get("/api/account/orders")
def account_orders(status: str = "all", p: Principal = Depends(current)):
    with session_scope() as s:
        return portfolio.orders(s, read_gateway(p.account_id), status if status in ("open", "all", "rejected") else "all")


@app.get("/api/market/instruments")
def market_instruments(q: str | None = None, p: Principal = Depends(current)):
    from countersign.core.verifier import list_instruments

    with session_scope() as s:
        return [instruments.handle(i).model_dump(mode="json") for i in list_instruments(s, q)]


@app.get("/api/market/quote/{symbol}")
def market_quote(symbol: str, p: Principal = Depends(current)):
    with session_scope() as s:
        q = portfolio.quote(s, symbol)
        if q is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": f"No unique instrument or live price for '{symbol}'."})
        return q


@app.get("/api/market/watchlist")
def market_watchlist(p: Principal = Depends(current)):
    with session_scope() as s:
        return portfolio.watchlist(s)


@app.get("/api/market/history/{symbol}")
def market_history(symbol: str, points: int = 300, p: Principal = Depends(current)):
    with session_scope() as s:
        r = instruments.resolve(s, symbol)
        if r.instrument is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": f"Unknown symbol '{symbol}'."})
        return market.history(r.instrument.instrument_id, max(10, min(points, 900)))


@app.get("/api/market/option-chain")
def market_option_chain(underlying: str = "NIFTY", expiry: str | None = None, width: int = 8, p: Principal = Depends(current)):
    with session_scope() as s:
        return portfolio.option_chain(s, expiry, max(2, min(width, 12)))


# ------------------------------------------------------------------ chat


class ChatBody(BaseModel):
    conversation_id: str | None = None
    message: str
    engine: str = "auto"


@app.post("/api/chat")
async def chat(body: ChatBody, p: Principal = Depends(current)):
    from countersign.llm.chat import stream

    if body.conversation_id:
        with session_scope() as s:
            foreign = s.execute(select(ChatMessage.id).where(ChatMessage.conversation_id == body.conversation_id, ChatMessage.user_id != p.user_id).limit(1)).first()
        if foreign is not None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such conversation."})

    async def gen():
        try:
            async for ev in stream(p.user_id, p.account_id, body.conversation_id, body.message, body.engine):
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'code': 'CHAT_FAILED', 'message': f'The assistant failed: {type(e).__name__}: {e}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/history")
def chat_history(conversation_id: str, p: Principal = Depends(current)):
    with session_scope() as s:
        rows = s.execute(select(ChatMessage).where(ChatMessage.conversation_id == conversation_id, ChatMessage.user_id == p.user_id).order_by(ChatMessage.created_at)).scalars().all()
        return [{"id": r.id, "role": r.role, "text": r.text, "cards": r.cards, "created_at": iso(r.created_at)} for r in rows]


# ------------------------------------------------------------------ proposals & countersign


@app.get("/api/proposals")
def list_proposals(kind: str | None = None, status: str | None = None, limit: int = 30, p: Principal = Depends(current)):
    with session_scope() as s:
        q = select(Proposal).where(Proposal.user_id == p.user_id)
        if kind:
            q = q.where(Proposal.kind == kind.upper())
        if status:
            q = q.where(Proposal.status == status.upper())
        rows = s.execute(q.order_by(Proposal.created_at.desc()).limit(max(1, min(limit, 100)))).scalars().all()
        return [view(s, r) for r in rows]


@app.get("/api/proposals/{pid}")
def get_proposal(pid: str, p: Principal = Depends(current)):
    with session_scope() as s:
        r = s.get(Proposal, pid)
        if r is None or r.user_id != p.user_id:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such proposal."})
        return view(s, r)


@app.post("/api/proposals/{pid}/reject")
def reject_proposal(pid: str, p: Principal = Depends(current)):
    with session_scope() as s:
        r = s.get(Proposal, pid)
        if r is None or r.user_id != p.user_id:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such proposal."})
        if r.status in ("AWAITING_APPROVAL", "BLOCKED"):
            r.status = "REJECTED_BY_USER"
            if r.kind == "MANDATE":
                m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == r.id)).scalar_one_or_none()
                if m:
                    m.state = "CANCELLED"
            if r.kind == "PLAN":
                pl = s.get(Plan, r.id)
                if pl:
                    pl.state = "REJECTED"
            audit.record(s, "trader", "PROPOSAL_REJECTED", "proposal", r.id, {})
            events.emit_proposal(r)
        return view(s, r)


class StartBody(BaseModel):
    method: str = "webauthn"


@app.post("/api/proposals/{pid}/approval/start")
def approval_start(pid: str, body: StartBody, request: Request, p: Principal = Depends(current)):
    with session_scope() as s:
        return cs.start(s, _user(s, p), pid, body.method, rp_id=rp_id_for(request.headers.get("origin")))


class CompleteBody(BaseModel):
    approval_id: str
    webauthn: dict | None = None
    pin: str | None = None
    totp: str | None = None


@app.post("/api/proposals/{pid}/approval/complete")
def approval_complete(pid: str, body: CompleteBody, p: Principal = Depends(current)):
    with session_scope() as s:
        prop = cs.complete(s, _user(s, p), pid, body.approval_id, webauthn=body.webauthn, pin=body.pin, totp=body.totp)
        return view(s, prop)


# ------------------------------------------------------------------ mandates, plans, outcomes


@app.get("/api/mandates")
def list_mandates(p: Principal = Depends(current)):
    with session_scope() as s:
        rows = s.execute(select(StandingMandate, Proposal).join(Proposal, Proposal.id == StandingMandate.proposal_id).where(StandingMandate.user_id == p.user_id).order_by(StandingMandate.created_at.desc())).all()
        return [mandate_view(m, pr.canonical) for m, pr in rows if m.state != "PENDING_APPROVAL" or pr.status == "AWAITING_APPROVAL"]


@app.post("/api/mandates/{mid}/cancel")
def cancel_mandate(mid: str, p: Principal = Depends(current)):
    with session_scope() as s:
        m = s.get(StandingMandate, mid)
        if m is None or m.user_id != p.user_id:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such standing instruction."})
        r = s.execute(text("UPDATE mandates SET state='CANCELLED', reason='Cancelled by trader' WHERE id=:id AND state IN ('ACTIVE','NEEDS_REVIEW','PENDING_APPROVAL') RETURNING id"), {"id": mid}).first()
        if r is None:
            raise HTTPException(409, {"code": "NOT_CANCELLABLE", "message": f"This instruction is {m.state.lower()} and cannot be cancelled."})
        if m.capability_id:
            s.execute(text("UPDATE capabilities SET status='REVOKED' WHERE id=:id AND status='ACTIVE'"), {"id": m.capability_id})
        prop = s.get(Proposal, m.proposal_id)
        prop.status = "CANCELLED"
        audit.record(s, "trader", "MANDATE_CANCELLED", "mandate", mid, {})
        s.flush()
        s.refresh(m)
        events.emit_mandate(m, "CANCELLED")
        return mandate_view(m, prop.canonical)


@app.get("/api/plans")
def list_plans(p: Principal = Depends(current)):
    return list_proposals(kind="PLAN", p=p)


@app.get("/api/outcomes")
def list_outcomes(limit: int = 50, p: Principal = Depends(current)):
    with session_scope() as s:
        rows = s.execute(select(OutcomeRow).where(OutcomeRow.account_id.in_([p.account_id, "*"])).order_by(OutcomeRow.ts.desc()).limit(max(1, min(limit, 200)))).scalars().all()
        return [{"id": r.id, "ts": iso(r.ts), "kind": r.kind, "title": r.title, "detail": r.detail, "severity": r.severity, "proposal_id": r.proposal_id, "broker_order_id": r.broker_order_id} for r in rows]


# ------------------------------------------------------------------ live events (SSE)


@app.get("/api/events")
async def live_events(request: Request, p: Principal = Depends(current)):
    async def gen():
        r = aioredis.from_url(settings().valkey_url, decode_responses=True)
        ps = r.pubsub()
        await ps.subscribe(events.CH_EVENTS, events.CH_TICKS)
        last_tick: dict[str, float] = {}
        last_ping = 0.0
        last_monitor = 0.0
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                now = time.time()
                if msg and msg.get("type") == "message":
                    if msg["channel"] == events.CH_TICKS:
                        t = json.loads(msg["data"])
                        if ":FO:" in t["instrument_id"]:
                            continue
                        if now - last_tick.get(t["instrument_id"], 0) < 0.9:
                            continue
                        last_tick[t["instrument_id"]] = now
                        tk = market.Tick.from_json(msg["data"])
                        yield f"event: tick\ndata: {json.dumps(tk.to_api())}\n\n"
                    else:
                        ev = json.loads(msg["data"])
                        data = ev.get("data", {})
                        if not events.visible_to(ev.get("type", ""), data, p.user_id, p.account_id):
                            continue
                        yield f"event: {ev['type']}\ndata: {json.dumps(data, default=str)}\n\n"
                if now - last_monitor > 2.0:
                    last_monitor = now
                    raw = await r.get("cs:monitor:status")
                    if raw:
                        yield f"event: monitor\ndata: {raw}\n\n"
                if now - last_ping > 15:
                    last_ping = now
                    yield "event: ping\ndata: {}\n\n"
        finally:
            await ps.unsubscribe()
            await ps.aclose()
            await r.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ audit & security


# System-wide audit subjects every trader may see (security events, invariant violations, kill switch, Attack Lab
# runs, presenter controls). Everything else is shown only to the user/account that owns its subject.
SYSTEM_AUDIT_SUBJECTS = ["security", "invariant", "system", "attack_run", "feed", "broker"]
_OWNED_AUDIT_SQL = """
    e.subject_type = ANY(:system_subjects)
    OR (e.subject_type = 'user' AND e.subject_id = ANY(:uids))
    OR (e.subject_type IN ('proposal', 'plan') AND e.subject_id IN (SELECT id FROM proposals WHERE user_id = ANY(:uids)))
    OR (e.subject_type = 'plan_step' AND split_part(e.subject_id, ':', 1) IN (SELECT id FROM proposals WHERE user_id = ANY(:uids)))
    OR (e.subject_type = 'approval' AND e.subject_id IN (SELECT id FROM approvals WHERE user_id = ANY(:uids)))
    OR (e.subject_type = 'capability' AND e.subject_id IN (SELECT id FROM capabilities WHERE account_id = ANY(:accts)))
    OR (e.subject_type = 'job' AND e.subject_id IN (SELECT j.id FROM exec_jobs j JOIN proposals pr ON pr.id = j.proposal_id WHERE pr.user_id = ANY(:uids)))
    OR (e.subject_type = 'outbox' AND e.subject_id IN (SELECT o.id FROM outbox o JOIN proposals pr ON pr.id = o.proposal_id WHERE pr.user_id = ANY(:uids)))
    OR (e.subject_type = 'mandate' AND e.subject_id IN (SELECT id FROM mandates WHERE user_id = ANY(:uids)))
    OR (e.subject_type = 'order' AND e.subject_id IN (SELECT broker_order_id FROM broker_orders WHERE account_id = ANY(:accts)))
    OR (e.subject_type = 'conversation' AND e.subject_id IN (SELECT conversation_id FROM chat_messages WHERE user_id = ANY(:uids)))
    OR (e.subject_type = 'session' AND e.subject_id IN (SELECT id FROM sessions WHERE user_id = ANY(:uids)))
    OR (e.subject_type = 'device' AND e.subject_id IN (SELECT id FROM device_credentials WHERE user_id = ANY(:uids)))
"""


@app.get("/api/audit")
def audit_list(limit: int = 100, before_id: int | None = None, p: Principal = Depends(current)):
    """The caller's own events plus system-wide security events. The Attack Lab sandbox (LAB1) is shared, so its
    activity is visible to every trader. Chain verification (/api/audit/verify) always covers the whole chain."""
    with session_scope() as s:
        lab = s.execute(select(User.id, Account.id).join(Account, Account.user_id == User.id).where(User.is_lab_user.is_(True))).all()
        params = {
            "system_subjects": SYSTEM_AUDIT_SUBJECTS,
            "uids": [p.user_id, *{u for u, _ in lab}],
            "accts": [p.account_id, *{a for _, a in lab}],
            "lim": max(1, min(limit, 500)),
        }
        cond = ""
        if before_id:
            cond = "e.id < :before_id AND "
            params["before_id"] = before_id
        rows = s.execute(
            text(f"SELECT e.id, e.ts, e.actor, e.event_type, e.subject_type, e.subject_id, e.data, e.prev_hash, e.hash FROM audit_events e WHERE {cond}({_OWNED_AUDIT_SQL}) ORDER BY e.id DESC LIMIT :lim"),
            params,
        ).all()
        return [{"id": r.id, "ts": iso(r.ts), "actor": r.actor, "event_type": r.event_type, "subject_type": r.subject_type, "subject_id": r.subject_id, "data": r.data, "prev_hash": r.prev_hash, "hash": r.hash} for r in rows]


@app.get("/api/audit/verify")
def audit_verify(p: Principal = Depends(current)):
    with session_scope() as s:
        return audit.verify_chain(s)


def _monitor_status() -> dict:
    raw = events.client().get("cs:monitor:status")
    if raw:
        d = json.loads(raw)
        d["healthy"] = d.get("healthy", False) and (time.time() - d.get("_ts", 0) < 10)
        return d
    return {"healthy": False, "last_heartbeat": None, "checks": [], "violations": 0, "unapproved_executions": 0}


@app.get("/api/security/status")
def security_status(p: Principal = Depends(current)):
    from countersign.llm.agent import availability, engine_label

    c = events.client()
    hb = c.get("cs:executor:heartbeat")
    feed_hb = c.get("cs:feed:heartbeat")
    ok, lat = broker_health()
    with session_scope() as s:
        ks = state.kill_switch(s)
    label = engine_label("auto")
    return {
        "kill_switch": ks,
        "monitor": _monitor_status(),
        "executor": {"healthy": bool(hb) and time.time() - float(hb) < 10, "last_heartbeat": iso(datetime.fromtimestamp(float(hb), UTC)) if hb else None, "holds_write_key": True},
        "api": {"holds_write_key": bool(settings().broker_write_key), "llm_has_order_tools": False},
        "broker": {"healthy": ok, "latency_ms": lat},
        "feed": {"healthy": bool(feed_hb) and time.time() - float(feed_hb) < 5, "last_tick_age_ms": int((time.time() - float(feed_hb)) * 1000) if feed_hb else None, "mode": "replay (simulated NSE, demo mode)"},
        "llm": {"engine": label, "available": availability()},
    }


@app.get("/api/security/events")
def security_events(scope: str = "mine", p: Principal = Depends(current)):
    """Events about the caller's own account plus system-wide ones (no account). scope=all also lists the drills on
    the shared Attack Lab sandbox. Other traders' accounts are never listed."""
    visible = [SecurityEvent.account_id == p.account_id, SecurityEvent.account_id.is_(None)]
    if scope == "all":
        visible.append(SecurityEvent.account_id == state.LAB_ACCOUNT)
    with session_scope() as s:
        rows = s.execute(select(SecurityEvent).where(or_(*visible)).order_by(SecurityEvent.ts.desc()).limit(100)).scalars().all()
        return [
            {
                "id": r.id,
                "ts": iso(r.ts),
                "severity": r.severity,
                "kind": r.kind,
                "message": r.message,
                "data": r.data,
                "resolved": r.resolved,
                "scope": "drill" if r.account_id == state.LAB_ACCOUNT else "system" if r.account_id is None else "account",
            }
            for r in rows
        ]


class KillBody(BaseModel):
    engaged: bool
    pin: str | None = None


@app.post("/api/security/kill-switch")
def kill_switch(body: KillBody, p: Principal = Depends(current)):
    with session_scope() as s:
        u = _user(s, p)
        if not body.engaged:
            # Releasing the global halt always needs the caller's own PIN (a trader who has not set one cannot).
            try:
                factors.verify_pin(s, u, body.pin or "")
            except CountersignError:
                s.commit()
                raise
        state.set_kill_switch(s, body.engaged, "Engaged by trader from the Security Center" if body.engaged else "Released by trader (PIN verified)", "trader")
    return security_status(p)


# ------------------------------------------------------------------ attack lab


class RunBody(BaseModel):
    scenario_id: str = "all"


@app.get("/api/attack-lab/scenarios")
def attack_scenarios(p: Principal = Depends(current)):
    from countersign.attack_lab.scenarios import SCENARIOS

    return [{"id": sc.id, "letter": sc.letter, "title": sc.title, "attack": sc.attack, "expected_guarantee": sc.guarantee} for sc in SCENARIOS]


@app.post("/api/attack-lab/run")
async def attack_run(body: RunBody, p: Principal = Depends(current)):
    from countersign.attack_lab.runner import run

    with session_scope() as s:
        if state.kill_switch(s)["engaged"]:
            # Scenarios need approvals, which the kill switch refuses; running them would only report errors.
            raise CountersignError("KILL_SWITCH_ENGAGED", "The kill switch is engaged, so the Attack Lab cannot run. Review the alert and release it in the Security Center first.")
    return await asyncio.to_thread(run, body.scenario_id)


@app.get("/api/attack-lab/runs")
def attack_runs(limit: int = 10, p: Principal = Depends(current)):
    from countersign.core.models import AttackRunRow

    with session_scope() as s:
        rows = s.execute(select(AttackRunRow).order_by(AttackRunRow.started_at.desc()).limit(limit)).scalars().all()
        return [{"id": r.id, "started_at": iso(r.started_at), "finished_at": iso(r.finished_at), "results": r.results, "scoreboard": r.scoreboard} for r in rows]


@app.get("/api/attack-lab/scoreboard")
def attack_scoreboard(p: Principal = Depends(current)):
    from countersign.attack_lab.runner import scoreboard

    return scoreboard()


# ------------------------------------------------------------------ demo controls


def _demo_only() -> None:
    if not settings().demo_mode:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Demo controls are disabled."})


@app.get("/api/demo/state")
def demo_state(p: Principal = Depends(current)):
    _demo_only()
    try:
        fault = BrokerAdmin().fault_state()
    except Exception:  # noqa: BLE001
        fault = {"mode": "unknown"}
    return {"feed_mode": "replay", "broker_fault": fault.get("mode"), "scenarios": ["calm", "dip_to_trigger", "spike", "crash", "stale"]}


class FeedBody(BaseModel):
    scenario: str
    symbol: str | None = None


@app.post("/api/demo/feed")
def demo_feed(body: FeedBody, p: Principal = Depends(current)):
    _demo_only()
    iid = None
    if body.symbol:
        with session_scope() as s:
            r = instruments.resolve(s, body.symbol)
            if r.instrument is None:
                raise HTTPException(404, {"code": "NOT_FOUND", "message": f"Unknown symbol '{body.symbol}'."})
            iid = r.instrument.instrument_id
    events.publish(events.CH_FEED_CONTROL, {"cmd": "scenario", "name": body.scenario, "instrument_id": iid})
    with session_scope() as s:
        audit.record(s, "presenter", "FEED_SCENARIO", "feed", iid or "all", {"scenario": body.scenario})
    return {"ok": True, "scenario": body.scenario, "instrument_id": iid}


class FaultBody(BaseModel):
    mode: str
    count: int = 1


@app.post("/api/demo/broker-fault")
def demo_fault(body: FaultBody, p: Principal = Depends(current)):
    _demo_only()
    r = BrokerAdmin().set_fault(body.mode, body.count)
    with session_scope() as s:
        audit.record(s, "presenter", "BROKER_FAULT_SET", "broker", "paper", {"mode": body.mode, "count": body.count})
    return r


@app.post("/api/demo/reset")
def demo_reset(p: Principal = Depends(current)):
    """Resets ONLY the caller: their paper account at the broker and their own proposals, approvals and execution
    records, mandates, plans, outcomes, order mirrors and chat. Other traders, the kill switch, security events and
    the audit chain are untouched. Also calms the replay feed, clears broker faults and refills the sandboxes."""
    _demo_only()
    admin = BrokerAdmin()
    # Broker orders go first, then the app rows that authorize them, so the monitor never sees a live order whose
    # countersignature was already deleted.
    admin.reset_account(p.account_id)
    with session_scope() as s:
        pids = list(s.execute(select(Proposal.id).where(Proposal.user_id == p.user_id)).scalars())
        convs = list(s.execute(select(ChatMessage.conversation_id).where(ChatMessage.user_id == p.user_id).distinct()).scalars())
        if pids:
            s.execute(delete(OutboxEntry).where(OutboxEntry.proposal_id.in_(pids)))
            s.execute(delete(ExecJob).where(ExecJob.proposal_id.in_(pids)))
            s.execute(delete(Capability).where(Capability.proposal_id.in_(pids)))
            s.execute(delete(Approval).where(Approval.proposal_id.in_(pids)))
            s.execute(delete(PlanStep).where(PlanStep.plan_id.in_(pids)))
            s.execute(delete(Plan).where(Plan.id.in_(pids)))
        s.execute(delete(StandingMandate).where(StandingMandate.user_id == p.user_id))
        s.execute(delete(Proposal).where(Proposal.user_id == p.user_id))
        s.execute(delete(OutcomeRow).where(OutcomeRow.account_id == p.account_id))
        mirrors = select(BrokerOrderMirror.broker_order_id).where(BrokerOrderMirror.account_id == p.account_id)
        s.execute(delete(OrderFill).where(OrderFill.broker_order_id.in_(mirrors)))
        s.execute(delete(BrokerOrderMirror).where(BrokerOrderMirror.account_id == p.account_id))
        s.execute(delete(ChatMessage).where(ChatMessage.user_id == p.user_id))
        audit.record(s, "trader", "DEMO_RESET", "user", p.user_id, {"account_id": p.account_id, "proposals_removed": len(pids), "note": "This trader's demo data was reset; audit chain retained."})
    from countersign.llm.chat import forget

    forget(p.user_id, convs)
    events.publish(events.CH_FEED_CONTROL, {"cmd": "scenario", "name": "calm"})
    admin.set_fault("none", 0)
    for sandbox in ("LAB1", settings().naive_account_id):
        admin.replenish(sandbox)
    return {"ok": True, "account_id": p.account_id}


# ------------------------------------------------------------------ public status (landing page, no auth)

_PUBLIC: dict = {"at": 0.0, "body": None}
_PUBLIC_LOCK = threading.Lock()


def _public_status_now() -> dict:
    now = time.time()
    try:
        c = events.client()
        hb = c.get("cs:executor:heartbeat")
        feed_hb = c.get("cs:feed:heartbeat")
        mon = _monitor_status()
    except Exception:  # noqa: BLE001
        hb, feed_hb, mon = None, None, {"healthy": False}
    broker_ok, _ = broker_health()
    systems = {
        "api": True,
        "broker": bool(broker_ok),
        "feed": bool(feed_hb) and now - float(feed_hb) < 5,
        "executor": bool(hb) and now - float(hb) < 10,
        "monitor": bool(mon.get("healthy")),
    }
    with session_scope() as s:
        runs = s.execute(select(AttackRunRow.started_at, AttackRunRow.scoreboard)).all()
        audit_n = s.execute(text("SELECT count(*) FROM audit_events")).scalar() or 0
    nifty_tick = market.latest("NSE:IDX:NIFTY")
    nifty = None
    if nifty_tick is not None:
        t = nifty_tick.to_api()
        nifty = {"ltp_paise": t["ltp_paise"], "change_bps": t["change_bps"]}
    return {
        "all_operational": all(systems.values()),
        "systems": systems,
        "unapproved_executions": int(mon.get("unapproved_executions", 0) or 0),
        "attacks_attempted": sum(int((sb or {}).get("run_attempted", 0)) for _, sb in runs),
        "attacks_blocked": sum(int((sb or {}).get("run_blocked", 0)) for _, sb in runs),
        "last_attack_run_at": iso(max((st for st, _ in runs), default=None)),
        "audit_events": int(audit_n),
        "market": {"mode": "replay", "nifty": nifty},
        "generated_at": iso(datetime.now(UTC)),
    }


@app.get("/api/public/status")
def public_status():
    """Aggregate, non-personal system status for the public landing page. Cached for about 2 s."""
    with _PUBLIC_LOCK:
        if _PUBLIC["body"] is not None and time.time() - _PUBLIC["at"] < 2.0:
            return _PUBLIC["body"]
    body = _public_status_now()
    with _PUBLIC_LOCK:
        _PUBLIC.update(at=time.time(), body=body)
    return body


@app.get("/api/health")
def health():
    return {"ok": True, "db": True, "valkey": events.ping(), "holds_write_key": bool(settings().broker_write_key)}


def main() -> None:
    import uvicorn

    # Keep-alive longer than the Next.js proxy keeps idle sockets, so a reused socket is never closed mid-request.
    # proxy_headers=False: uvicorn would otherwise replace the client address with X-Forwarded-For for loopback peers,
    # and the Next.js proxy passes that header through from the browser unchanged (see client_ip).
    uvicorn.run("countersign.api.main:app", host="127.0.0.1", port=8000, log_level="warning", timeout_keep_alive=75, proxy_headers=False)


if __name__ == "__main__":
    main()
