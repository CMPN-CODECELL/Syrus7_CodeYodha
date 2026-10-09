"""Creates schema and seeds instruments, the demo trader, and the Attack Lab user (with its virtual authenticator)."""

from __future__ import annotations

from sqlalchemy import select

from countersign.auth.factors import user_handle
from countersign.core import audit, instruments
from countersign.core.db import session_scope
from countersign.core.models import Account, AuditEvent, User, create_all

DEMO_USER = ("demo", "Demo Trader", "DEMO1")
LAB_USER = ("attack-lab", "Attack Lab (automated)", "LAB1")


def ensure_user(s, username: str, display: str, account_id: str, lab: bool = False) -> User:
    u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if u is None:
        u = User(username=username, display_name=display, webauthn_user_handle=user_handle(username), is_lab_user=lab)
        s.add(u)
        s.flush()
    if s.get(Account, account_id) is None:
        s.add(Account(id=account_id, user_id=u.id, label=display, segments=["EQ", "FO"], trading_enabled=True))
    return u


def bootstrap() -> None:
    create_all()
    with session_scope() as s:
        n = instruments.seed(s)
        demo = ensure_user(s, *DEMO_USER)
        lab = ensure_user(s, *LAB_USER, lab=True)
        if s.execute(select(AuditEvent.id).limit(1)).first() is None:
            audit.record(s, "system", "GENESIS", "system", "countersign", {"note": "Audit chain started"})
        if n:
            audit.record(s, "system", "INSTRUMENT_MASTER_LOADED", "system", "instruments", {"count": n})
        from countersign.auth.virtual_authenticator import ensure_registered

        ensure_registered(s, lab.id)
        if lab.pin_hash is None:
            from countersign.auth.factors import _ph

            lab.pin_hash = _ph.hash("2468")
        _ = demo


if __name__ == "__main__":
    bootstrap()
    print("bootstrap ok")
