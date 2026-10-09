"""Fallback factors. PIN: 4 digits, Argon2id, exponential backoff, lockout after repeated failures, never logged.
TOTP: RFC 6238 authenticator app (free, offline), secret encrypted at rest, replay of a used code rejected.
Account password: Argon2id, used only to sign in and to recover the PIN; it can never approve a trade."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from countersign.core.approval_crypto import CountersignError
from countersign.core.config import REPO_ROOT, settings
from countersign.core.models import User

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def is_weak_pin(pin: str) -> bool:
    """All one digit (1111) or a straight ascending or descending run (1234, 0123, 4321, 9876)."""
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:], strict=False)}
    return steps in ({0}, {1}, {-1})


_PIN_RE = re.compile(r"[0-9]{4}")
_CODE_RE = re.compile(r"[0-9]{6}")


def check_pin_strength(pin: str) -> None:
    # ASCII digits only: str.isdigit() also accepts superscripts and other scripts' digits, which no keypad can type.
    if not isinstance(pin, str) or not _PIN_RE.fullmatch(pin):
        raise CountersignError("PIN_FORMAT", "The app PIN must be exactly 4 digits.")
    if is_weak_pin(pin):
        raise CountersignError("WEAK_PIN", "That PIN is too easy to guess (repeated digits or a straight run like 1234). Choose a different 4-digit PIN.")


def set_pin(s: Session, user: User, pin: str, current_pin: str | None = None) -> None:
    check_pin_strength(pin)
    if user.pin_hash:
        if current_pin is None:
            raise CountersignError("PIN_REQUIRED", "Enter your current PIN to change it.")
        verify_pin(s, user, current_pin)
    user.pin_hash = _ph.hash(pin)
    user.pin_failed_count = 0
    user.pin_locked_until = None


def reset_pin(user: User, new_pin: str) -> None:
    """Sets a new PIN after the caller proved the account password (and authenticator code when enabled)."""
    check_pin_strength(new_pin)
    user.pin_hash = _ph.hash(new_pin)
    user.pin_failed_count = 0
    user.pin_locked_until = None


def hash_password(password: str) -> str:
    return _ph.hash(password)


# Verified against when the email is unknown, so a miss costs the same time as a wrong password.
_DUMMY_HASH = _ph.hash("countersign-timing-equaliser")


def check_password(password_hash: str | None, password: str) -> bool:
    try:
        return _ph.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001
        return False


def verify_pin(s: Session, user: User, pin: str) -> None:
    now = datetime.now(UTC)
    if not user.pin_hash:
        raise CountersignError("PIN_NOT_SET", "No app PIN is set. Set one in the Security Center first.")
    if user.pin_locked_until and user.pin_locked_until > now:
        secs = int((user.pin_locked_until - now).total_seconds()) + 1
        raise CountersignError("PIN_LOCKED", f"PIN approvals are locked for {secs}s after repeated wrong attempts. Use your passkey or authenticator code.")
    try:
        _ph.verify(user.pin_hash, pin)
    except VerifyMismatchError:
        user.pin_failed_count = (user.pin_failed_count or 0) + 1
        n = user.pin_failed_count
        if n >= settings().pin_max_attempts:
            user.pin_locked_until = now + timedelta(minutes=15)
            msg = "Too many wrong PIN attempts. PIN approvals are locked for 15 minutes; use your passkey or authenticator code."
        elif n >= 3:
            wait = 5 * 2 ** (n - 3)
            user.pin_locked_until = now + timedelta(seconds=wait)
            msg = f"Wrong PIN. Try again in {wait}s ({settings().pin_max_attempts - n} attempts left before lockout)."
        else:
            msg = f"Wrong PIN ({settings().pin_max_attempts - n} attempts left)."
        s.flush()
        raise CountersignError("PIN_INVALID", msg) from None
    user.pin_failed_count = 0
    user.pin_locked_until = None


def _fernet() -> Fernet:
    key = settings().totp_encryption_key
    if not key:
        p = Path(REPO_ROOT / ".secrets" / "totp.key")
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(Fernet.generate_key())
        key = p.read_text().strip()
    return Fernet(key.encode() if isinstance(key, str) else key)


def totp_setup(user: User) -> tuple[str, str]:
    """Generates a new secret and keeps it pending: a linked authenticator stays active until the new one is confirmed."""
    secret = pyotp.random_base32()
    user.totp_pending_enc = _fernet().encrypt(secret.encode()).decode()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email or user.username, issuer_name="Countersign")
    return uri, secret


def _decrypt(enc: str) -> str:
    return _fernet().decrypt(enc.encode()).decode()


def verify_totp(user: User, code: str, *, confirm: bool = False) -> None:
    """Checks a current code. With confirm=True it checks the pending secret and, on success, makes it the active one."""
    code = (code or "").strip().replace(" ", "")
    if not _CODE_RE.fullmatch(code):
        raise CountersignError("TOTP_INVALID", "Enter the 6-digit code from your authenticator app.")
    if confirm:
        # An older unconfirmed setup kept its secret in totp_secret_enc; a confirmed one is never re-confirmed.
        enc = user.totp_pending_enc or (None if user.totp_confirmed else user.totp_secret_enc)
        if not enc:
            raise CountersignError("TOTP_NOT_SET", "Start authenticator setup first.")
    else:
        if not user.totp_confirmed or not user.totp_secret_enc:
            raise CountersignError("TOTP_NOT_SET", "No authenticator app is set up. Set one up in the Security Center.")
        enc = user.totp_secret_enc
    t = pyotp.TOTP(_decrypt(enc))
    now = datetime.now(UTC).timestamp()
    for offset in (0, -30, 30):
        counter = int((now + offset) // 30)
        if t.at(counter * 30) == code:
            if not confirm and user.totp_last_counter is not None and counter <= user.totp_last_counter:
                raise CountersignError("TOTP_REPLAY", "That code was already used. Wait for the next code.")
            user.totp_last_counter = max(counter, user.totp_last_counter or 0)
            if confirm:
                user.totp_secret_enc = enc
                user.totp_pending_enc = None
                user.totp_confirmed = True
            return
    raise CountersignError("TOTP_INVALID", "That authenticator code is not valid. Check the time on your device and try the current code.")


def user_handle(username: str) -> bytes:
    return hashlib.sha256(f"countersign-user:{username}".encode()).digest()


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
