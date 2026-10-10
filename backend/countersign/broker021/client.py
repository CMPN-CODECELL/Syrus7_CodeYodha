"""HTTP client for the 021 REST API. Lives only in the gateway021 process.

Token: one live token per account (a new login revokes the previous one), so logins are single-flight and the token
is cached until expiresAt. A 401 triggers exactly one re-login; a second 401 fails closed.
Reads (GET) are idempotent and are retried with exponential backoff and jitter on 429, 5xx, timeouts and dropped
connections. Writes (POST, PUT, DELETE) are sent exactly once and NEVER retried here: an ambiguous write outcome is
returned as kind "unknown" and the caller reconciles against the order book."""

from __future__ import annotations

import email.utils
import json
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from countersign.broker021.mapping import AMBIGUOUS_HTTP, DEFINITE_REJECT_HTTP
from countersign.broker021.settings import GatewaySettings

RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class Api021Error(Exception):
    """A definite error answer from 021 (status and the error text from the envelope)."""

    def __init__(self, status: int, error: str):
        super().__init__(f"021 HTTP {status}: {error}")
        self.status = status
        self.error = error


class Api021Unavailable(Exception):
    """021 did not give a usable answer after the read retries (rate limits, 5xx, timeouts)."""


class LoginFailed(Exception):
    pass


@dataclass
class WriteResult:
    kind: str  # ok | rejected | unknown | not_sent | auth
    status: int | None = None
    data: dict | None = None
    error: str | None = None
    sent_at: float = 0.0  # local time just before the request was sent
    elapsed_s: float = 0.0


@dataclass
class Stats:
    logins: int = 0
    relogins_on_401: int = 0
    read_retries: int = 0
    http_429: int = 0
    http_5xx: int = 0
    timeouts: int = 0
    last_error: str | None = None
    last_error_at: float | None = None
    last_ok_at: float | None = None
    by_status: dict = field(default_factory=dict)

    def current_error(self) -> str | None:
        """The last failure, unless a call has succeeded since (older blips were already recovered from)."""
        return self.last_error if (self.last_error_at or 0) > (self.last_ok_at or 0) else None


def error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
    except (ValueError, json.JSONDecodeError):
        return (r.text or "").strip()[:300] or f"HTTP {r.status_code}"
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or body)[:300]
    return str(body)[:300]


def login_failure(status: int) -> str:
    """A fixed message for a refused 021 login that keeps only the HTTP status: 021's own error text is never passed on
    (to the API, the status heartbeat or a browser)."""
    if status in (401, 403):
        return f"021 did not accept the UCC and password (HTTP {status})."
    if status == 429:
        return "021 is limiting logins right now (HTTP 429)."
    if status >= 500:
        return f"021 could not process the login (HTTP {status})."
    return f"021 refused the login (HTTP {status})."


class Client021:
    def __init__(self, cfg: GatewaySettings, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep, password: Callable[[], str] | None = None, stop_on_rejected: bool = False):
        self.cfg = cfg
        self._sleep = sleep
        # The .env account reads its password from settings; a self-service linked account unseals its stored password
        # only for the moment of a login (vault.py), so the clear password is not kept on this object.
        self._password = password or (lambda: cfg.password.get_secret_value())
        self.stop_on_rejected = stop_on_rejected  # linked accounts: a refused stored password is never tried again
        self.credentials_rejected = False
        self._http = httpx.Client(base_url=cfg.rest_base, timeout=cfg.read_timeout_s, transport=transport, headers={"Accept": "application/json"})
        self._token: str | None = None
        self._expires_at = 0.0
        self._login_lock = threading.Lock()
        self.clock_offset_s = 0.0  # 021 server time minus local time, from the Date header
        self.stats = Stats()
        self.drop_next_write_responses = 0  # fault drill only (gateway /admin/faults, test_faults=true)
        self.fail_next_write_connects = 0  # fault drill only: the next order write fails before any byte is sent

    # ------------------------------------------------------------------ token

    @property
    def token_expires_at(self) -> float:
        return self._expires_at

    @property
    def logged_in(self) -> bool:
        return bool(self._token) and time.time() < self._expires_at

    def token(self) -> str:
        with self._login_lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            self._login()
            assert self._token is not None
            return self._token

    def _login(self) -> None:
        if self.credentials_rejected:
            raise LoginFailed("021 refused the stored password for this account; connect it again with the current password.")
        password = self._password() if self.cfg.ucc else ""
        if not password:
            raise LoginFailed("021 credentials are not configured (CS_021_UCC / CS_021_PASSWORD).")
        try:
            r = self._http.post("/auth/token", json={"username": self.cfg.ucc, "password": password})
        except httpx.HTTPError as e:
            raise LoginFailed(f"021 login request failed: {type(e).__name__}") from None
        self._note_clock(r)
        if r.status_code != 200:
            if r.status_code in (401, 403) and self.stop_on_rejected:
                self.credentials_rejected = True  # never retry a refused password on its own: it could lock the account
            raise LoginFailed(login_failure(r.status_code))
        if not self._install(r):
            raise LoginFailed("021 login answered without an access token.")

    def _install(self, r: httpx.Response) -> bool:
        try:
            data = (r.json() or {}).get("data") or {}
        except ValueError:
            return False
        tok = data.get("accessToken") if isinstance(data, dict) else None
        if not tok:
            return False
        exp = time.time() + float(data.get("expiresIn") or 3600)
        try:
            exp = datetime.fromisoformat(str(data["expiresAt"])).timestamp()
        except (KeyError, ValueError):
            pass
        self._token, self._expires_at = tok, exp
        self.stats.logins += 1
        return True

    def verify_login(self, password: str) -> tuple[str, str | None]:
        """One login with a password the trader just entered (self-service linking). It goes through this account's
        single-flight lock, so on success the new token replaces this client's own token (021 keeps one live token per
        account). Returns (kind, detail): ok | rejected (401 / 403: wrong UCC or password) | refused (other 4xx) |
        unavailable (429, 5xx, timeout, no token). Never retried. detail is only "HTTP <status>" or an exception class
        name: never the password and never 021's error text."""
        with self._login_lock:
            try:
                r = self._http.post("/auth/token", json={"username": self.cfg.ucc, "password": password})
            except httpx.HTTPError as e:
                return "unavailable", type(e).__name__
            self._note_clock(r)
            if r.status_code in (401, 403):
                return "rejected", f"HTTP {r.status_code}"
            if r.status_code == 429 or r.status_code >= 500:
                return "unavailable", f"HTTP {r.status_code}"
            if r.status_code != 200:
                return "refused", f"HTTP {r.status_code}"
            if not self._install(r):
                return "unavailable", "021 answered without an access token"
            self.credentials_rejected = False
            return "ok", None

    def drop_token(self) -> None:
        """Forgets the access token (unlink). 021 has no logout endpoint; the token simply ends at 05:00 IST."""
        with self._login_lock:
            self._token = None
            self._expires_at = 0.0

    def _invalidate(self, used: str) -> None:
        with self._login_lock:
            if self._token == used:
                self._token = None
                self._expires_at = 0.0

    # ------------------------------------------------------------------ helpers

    def now(self) -> float:
        """Current time on 021's clock (local time corrected by the measured offset)."""
        return time.time() + self.clock_offset_s

    def _note_clock(self, r: httpx.Response) -> None:
        d = r.headers.get("date")
        if not d:
            return
        try:
            server = email.utils.parsedate_to_datetime(d).timestamp()
        except (TypeError, ValueError):
            return
        # The Date header has 1 s resolution; smooth it so one rounding does not swing the offset.
        off = server - time.time()
        self.clock_offset_s = off if self.stats.logins == 0 and self.clock_offset_s == 0 else 0.8 * self.clock_offset_s + 0.2 * off

    def _note_error(self, text: str) -> None:
        self.stats.last_error = text[:300]
        self.stats.last_error_at = time.time()

    def _backoff(self, attempt: int) -> None:
        base = min(4.0, 0.4 * (2**attempt))
        self._sleep(base + random.uniform(0, base))

    # ------------------------------------------------------------------ reads (idempotent, retried)

    def get(self, path: str, params: dict | None = None, *, raw: bool = False) -> Any:
        relogged = False
        attempt = 0
        last = "no attempt"
        while attempt <= self.cfg.read_retries:
            tok = self.token()
            try:
                r = self._http.get(path, params=params, headers={"Authorization": f"Bearer {tok}"})
            except httpx.TimeoutException as e:
                self.stats.timeouts += 1
                last = f"timeout ({type(e).__name__})"
            except httpx.TransportError as e:
                last = f"connection error ({type(e).__name__})"
            else:
                self._note_clock(r)
                self.stats.by_status[r.status_code] = self.stats.by_status.get(r.status_code, 0) + 1
                if r.status_code < 300:
                    self.stats.last_ok_at = time.time()
                if r.status_code == 401 and not relogged:
                    relogged = True
                    self.stats.relogins_on_401 += 1
                    self._invalidate(tok)
                    continue
                if r.status_code in RETRYABLE_HTTP:
                    if r.status_code == 429:
                        self.stats.http_429 += 1
                    else:
                        self.stats.http_5xx += 1
                    last = f"HTTP {r.status_code} {error_text(r)}"
                elif r.status_code >= 400:
                    err = error_text(r)
                    if r.status_code == 401:
                        self._invalidate(tok)
                    raise Api021Error(r.status_code, err)
                else:
                    if raw:
                        return r
                    return r.json()
            self._note_error(f"GET {path}: {last}")
            attempt += 1
            if attempt <= self.cfg.read_retries:
                self.stats.read_retries += 1
                self._backoff(attempt - 1)
        raise Api021Unavailable(f"GET {path} failed after {self.cfg.read_retries + 1} attempts: {last}")

    # ------------------------------------------------------------------ writes (sent once, never retried)

    def write(self, method: str, path: str, body: dict) -> WriteResult:
        try:
            tok = self.token()
        except LoginFailed as e:
            return WriteResult(kind="not_sent", error=str(e))
        sent_at = time.time()
        t0 = time.perf_counter()
        if self.fail_next_write_connects > 0 and method == "POST":
            # Fault drill: the network drops before the connection is made, so nothing reaches 021.
            self.fail_next_write_connects -= 1
            self._note_error(f"{method} {path}: not sent (fault drill)")
            return WriteResult(kind="not_sent", error="Connection to 021 failed before the request was sent (fault drill: simulated network drop).", sent_at=sent_at, elapsed_s=0.0)
        try:
            r = self._http.request(method, path, json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=self.cfg.write_timeout_s)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            # The connection was never established, so no request bytes reached 021.
            self._note_error(f"{method} {path}: not sent ({type(e).__name__})")
            return WriteResult(kind="not_sent", error=f"Connection to 021 failed before the request was sent ({type(e).__name__}).", sent_at=sent_at, elapsed_s=time.perf_counter() - t0)
        except httpx.TimeoutException as e:
            self.stats.timeouts += 1
            self._note_error(f"{method} {path}: timeout")
            return WriteResult(kind="unknown", error=f"021 did not answer within {self.cfg.write_timeout_s:.0f} s ({type(e).__name__}).", sent_at=sent_at, elapsed_s=time.perf_counter() - t0)
        except httpx.TransportError as e:
            self._note_error(f"{method} {path}: connection dropped")
            return WriteResult(kind="unknown", error=f"The connection dropped before 021's answer arrived ({type(e).__name__}).", sent_at=sent_at, elapsed_s=time.perf_counter() - t0)
        elapsed = time.perf_counter() - t0
        self._note_clock(r)
        self.stats.by_status[r.status_code] = self.stats.by_status.get(r.status_code, 0) + 1
        err = error_text(r) if r.status_code != 200 else None
        if r.status_code == 200:
            self.stats.last_ok_at = time.time()
            try:
                env = r.json()
            except ValueError:
                return WriteResult(kind="unknown", status=200, error="021 answered 200 with a body that is not JSON.", sent_at=sent_at, elapsed_s=elapsed)
            if self.drop_next_write_responses > 0 and method == "POST":
                self.drop_next_write_responses -= 1
                return WriteResult(kind="unknown", status=None, error="Response dropped after 021 accepted the order (fault drill).", sent_at=sent_at, elapsed_s=elapsed)
            if isinstance(env, dict) and env.get("success") is False:
                return WriteResult(kind="rejected", status=200, data=env.get("data"), error=str(env.get("error") or "Rejected by 021."), sent_at=sent_at, elapsed_s=elapsed)
            return WriteResult(kind="ok", status=200, data=(env or {}).get("data") if isinstance(env, dict) else None, sent_at=sent_at, elapsed_s=elapsed)
        if r.status_code == 401:
            self._invalidate(tok)
            self._note_error(f"{method} {path}: HTTP 401 {err}")
            return WriteResult(kind="auth", status=401, error=err, sent_at=sent_at, elapsed_s=elapsed)
        if r.status_code in DEFINITE_REJECT_HTTP:
            return WriteResult(kind="rejected", status=r.status_code, error=err, sent_at=sent_at, elapsed_s=elapsed)
        if r.status_code in AMBIGUOUS_HTTP or r.status_code >= 500:
            if r.status_code == 429:
                self.stats.http_429 += 1
            else:
                self.stats.http_5xx += 1
            self._note_error(f"{method} {path}: HTTP {r.status_code} {err}")
            return WriteResult(kind="unknown", status=r.status_code, error=err, sent_at=sent_at, elapsed_s=elapsed)
        return WriteResult(kind="rejected", status=r.status_code, error=err, sent_at=sent_at, elapsed_s=elapsed)

    def close(self) -> None:
        self._http.close()
