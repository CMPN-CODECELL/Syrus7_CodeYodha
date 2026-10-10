"""The gateway's own records (broker database, next to the paper broker's tables): one row per placement attempt, and
the sealed credentials of self-service linked 021 accounts.

021 has no client order tag, so the gateway keeps tag -> 021 orderId itself. The row is written BEFORE the order is
sent (write-ahead), so a tag is never sent twice, and an ambiguous outcome is always reconciled from a known send time.
g021_credentials holds one row per (UCC, linking Countersign user): the password sealed with the gateway's AES-GCM
data key (vault.py), never in clear, and only once the API confirmed the link. A freshly verified password first waits in
g021_pending_credentials until the API attached the account and confirmed (POST /admin/link/confirm); an unconfirmed one
is deleted after PENDING_TTL_S and is never used to log in. g021_link_failures records failed verification logins per
UCC (the per-UCC budget that stops anyone from driving wrong-password logins at one 021 account). All tables are created
with CREATE TABLE IF NOT EXISTS (metadata.create_all)."""

from __future__ import annotations

import time
from functools import lru_cache

from sqlalchemy import BigInteger, Float, Integer, String, Text, create_engine, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from countersign.core.config import settings

# SENDING: written, request in flight or being reconciled. ACCEPTED: mapped to a 021 order. REJECTED: 021 refused it
# (possibly mapped to its Rejected row). NOT_PLACED: confirmed absent from 021's order book. AMBIGUOUS: several
# matching orders, never guessed. UNCONFIRMED: the order book could not be read in time, so presence is unknown.
OPEN_STATES = ("SENDING",)
REVIEW_STATES = ("AMBIGUOUS", "UNCONFIRMED")


class G(DeclarativeBase):
    pass


class Attempt(G):
    __tablename__ = "g021_attempts"
    tag: Mapped[str] = mapped_column(String(20), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(160), index=True)
    request: Mapped[dict] = mapped_column(JSONB)  # the Countersign order as received (paper /v1 shape)
    order_021: Mapped[dict] = mapped_column(JSONB)  # exactly what was sent to 021
    state: Mapped[str] = mapped_column(String(16))
    order_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    candidates: Mapped[list] = mapped_column(JSONB, default=list)
    note: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[float] = mapped_column(Float)
    resolved_at: Mapped[float | None] = mapped_column(Float)
    created_ms: Mapped[int] = mapped_column(BigInteger)


class Credential(G):
    __tablename__ = "g021_credentials"
    ucc: Mapped[str] = mapped_column(String(32), primary_key=True)
    linked_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_enc: Mapped[str] = mapped_column(Text)  # AES-256-GCM (12-byte nonce + ciphertext + tag), base64
    created_at: Mapped[float] = mapped_column(Float)


class PendingCredential(G):
    """A verified password waiting for the API to confirm the link. Never read by a login."""

    __tablename__ = "g021_pending_credentials"
    ucc: Mapped[str] = mapped_column(String(32), primary_key=True)
    linked_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class LinkFailure(G):
    """One failed verification login (021 refused the UCC and password) during self-service linking."""

    __tablename__ = "g021_link_failures"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ucc: Mapped[str] = mapped_column(String(32), index=True)
    at: Mapped[float] = mapped_column(Float)


@lru_cache
def _engine():
    return create_engine(settings().broker_database_url, pool_pre_ping=True, pool_size=5, future=True)


@lru_cache
def _sf():
    return sessionmaker(bind=_engine(), expire_on_commit=False)


def init_db() -> None:
    G.metadata.create_all(_engine())


def as_dict(a: Attempt | None) -> dict | None:
    if a is None:
        return None
    return {
        "tag": a.tag,
        "account_id": a.account_id,
        "state": a.state,
        "broker_order_id": a.order_id,
        "http_status": a.http_status,
        "error": a.error,
        "candidates": list(a.candidates or []),
        "note": a.note,
        "sent_at": a.sent_at,
        "resolved_at": a.resolved_at,
        "request": dict(a.request or {}),
        "order_021": dict(a.order_021 or {}),
        "fingerprint": a.fingerprint,
    }


def get(tag: str) -> dict | None:
    with _sf()() as s:
        return as_dict(s.get(Attempt, tag))


def by_order_id(order_id: str) -> dict | None:
    if str(order_id or "") in ("", "0", "None"):
        return None  # 021's rejected orders are all order 0: never one of ours by id
    with _sf()() as s:
        return as_dict(s.execute(select(Attempt).where(Attempt.order_id == str(order_id))).scalar_one_or_none())


def create(tag: str, account_id: str, fp: str, request: dict, order_021: dict) -> None:
    with _sf()() as s:
        s.add(Attempt(tag=tag, account_id=account_id, fingerprint=fp, request=request, order_021=order_021, state="SENDING", sent_at=time.time(), candidates=[], created_ms=int(time.time() * 1000)))
        s.commit()


def update(tag: str, **kw) -> None:
    if "order_id" in kw and str(kw["order_id"] or "") in ("", "0", "None"):
        kw["order_id"] = None  # 021 lists every rejected order as order 0: that is not an id, and it is not unique
    with _sf()() as s:
        a = s.get(Attempt, tag)
        if a is None:
            return
        for k, v in kw.items():
            setattr(a, k, v)
        if "state" in kw and kw["state"] not in OPEN_STATES:
            a.resolved_at = time.time()
        s.commit()


NEVER_SENT = "never sent: the connection failed before any byte reached 021"


def holding_fingerprint(fp: str, account_id: str, hold_s: float) -> dict | None:
    """An unresolved placement with this fingerprint (in flight, needing review, or resolved as not placed only
    moments ago and so still able to appear late). A second identical placement is refused while one exists."""
    now = time.time()
    with _sf()() as s:
        rows = s.execute(select(Attempt).where(Attempt.fingerprint == fp, Attempt.account_id == account_id, Attempt.state.in_(["SENDING", "NOT_PLACED", "AMBIGUOUS", "UNCONFIRMED"]))).scalars().all()
        for a in rows:
            if a.state == "SENDING" and now - a.sent_at < 600:
                return as_dict(a)
            if a.state == "NOT_PLACED" and a.note == NEVER_SENT:
                continue  # nothing reached 021, so it cannot appear late: countersigning again may send at once
            if a.state in ("NOT_PLACED", "AMBIGUOUS", "UNCONFIRMED") and a.resolved_at and now - a.resolved_at < hold_s:
                return as_dict(a)
    return None


def mapped_order_ids(account_id: str) -> set[str]:
    with _sf()() as s:
        return {x for x in s.execute(select(Attempt.order_id).where(Attempt.account_id == account_id, Attempt.order_id.is_not(None), Attempt.order_id != "0")).scalars()}


def tags_by_order(account_id: str) -> dict[str, dict]:
    with _sf()() as s:
        return {a.order_id: as_dict(a) for a in s.execute(select(Attempt).where(Attempt.account_id == account_id, Attempt.order_id.is_not(None), Attempt.order_id != "0")).scalars()}


def late_candidates(account_id: str, window_s: float) -> list[dict]:
    """Placements reported as not placed (or unconfirmed) recently: 021 may still show them late."""
    since = time.time() - window_s
    with _sf()() as s:
        rows = s.execute(select(Attempt).where(Attempt.account_id == account_id, Attempt.state.in_(["NOT_PLACED", "UNCONFIRMED"]), Attempt.order_id.is_(None), Attempt.sent_at >= since)).scalars().all()
        return [as_dict(a) for a in rows if a.note != NEVER_SENT]


def sending(account_id: str) -> list[dict]:
    """Placements still in flight (sent, answer not yet resolved)."""
    with _sf()() as s:
        rows = s.execute(select(Attempt).where(Attempt.account_id == account_id, Attempt.state == "SENDING")).scalars().all()
        return [as_dict(a) for a in rows]


def stale_sending(account_id: str, older_than_s: float) -> list[dict]:
    """Placements left SENDING by a crash of the gateway itself; they are reconciled on startup."""
    before = time.time() - older_than_s
    with _sf()() as s:
        rows = s.execute(select(Attempt).where(Attempt.account_id == account_id, Attempt.state == "SENDING", Attempt.sent_at < before)).scalars().all()
        return [as_dict(a) for a in rows]


def rejected_since(since: float) -> list[dict]:
    """REJECTED attempts sent at or after since, on every account (021 circuit limits are per instrument)."""
    with _sf()() as s:
        rows = s.execute(select(Attempt).where(Attempt.state == "REJECTED", Attempt.sent_at >= since).order_by(Attempt.sent_at)).scalars().all()
        return [as_dict(a) for a in rows]


def delete_tags(prefix: str) -> None:
    """Test helper: removes attempts whose tag starts with prefix."""
    with _sf()() as s:
        s.query(Attempt).filter(Attempt.tag.like(prefix + "%")).delete(synchronize_session=False)
        s.commit()


# ------------------------------------------------------------------ sealed credentials of linked accounts

PENDING_TTL_S = 120.0  # an unconfirmed link is deleted (and its session dropped) after this long


def save_credential(ucc: str, user_id: str, password_enc: str) -> None:
    with _sf()() as s:
        row = s.get(Credential, (ucc, user_id))
        if row is None:
            s.add(Credential(ucc=ucc, linked_user_id=user_id, password_enc=password_enc, created_at=time.time()))
        else:
            row.password_enc, row.created_at = password_enc, time.time()
        s.commit()


def credentials(ucc: str) -> list[tuple[str, str]]:
    """(linked user id, sealed password) of every CONFIRMED link of this UCC, newest first. Pending (unconfirmed)
    links live in another table and are never returned."""
    with _sf()() as s:
        rows = s.execute(select(Credential).where(Credential.ucc == ucc).order_by(Credential.created_at.desc())).scalars().all()
        return [(r.linked_user_id, r.password_enc) for r in rows]


def latest_credential(ucc: str) -> tuple[str, str] | None:
    """(linked user id, sealed password) of the most recent confirmed link of this UCC, or None."""
    rows = credentials(ucc)
    return rows[0] if rows else None


def linked_uccs() -> list[str]:
    """UCCs with at least one confirmed link (the accounts the gateway serves after a start)."""
    with _sf()() as s:
        return sorted(set(s.execute(select(Credential.ucc)).scalars()))


def holders(ucc: str) -> set[str]:
    """Users holding a confirmed or a still-pending (not expired) link of this UCC."""
    fresh = time.time() - PENDING_TTL_S
    with _sf()() as s:
        done = set(s.execute(select(Credential.linked_user_id).where(Credential.ucc == ucc)).scalars())
        pending = set(s.execute(select(PendingCredential.linked_user_id).where(PendingCredential.ucc == ucc, PendingCredential.created_at >= fresh)).scalars())
    return done | pending


def has_credential(ucc: str) -> bool:
    return latest_credential(ucc) is not None


def delete_credential(ucc: str, user_id: str) -> int:
    with _sf()() as s:
        n = s.execute(delete(Credential).where(Credential.ucc == ucc, Credential.linked_user_id == user_id)).rowcount
        s.commit()
        return int(n or 0)


def save_pending(ucc: str, user_id: str, password_enc: str) -> None:
    with _sf()() as s:
        row = s.get(PendingCredential, (ucc, user_id))
        if row is None:
            s.add(PendingCredential(ucc=ucc, linked_user_id=user_id, password_enc=password_enc, created_at=time.time()))
        else:
            row.password_enc, row.created_at = password_enc, time.time()
        s.commit()


def confirm_pending(ucc: str, user_id: str, now: float | None = None) -> bool:
    """Moves a pending link into g021_credentials (replacing that user's earlier password for the UCC). False when
    there is no pending link or it is older than PENDING_TTL_S (it is then deleted, never confirmed)."""
    now = time.time() if now is None else now
    with _sf()() as s:
        row = s.execute(select(PendingCredential).where(PendingCredential.ucc == ucc, PendingCredential.linked_user_id == user_id).with_for_update()).scalar_one_or_none()
        if row is None:
            return False
        enc, fresh = row.password_enc, now - row.created_at <= PENDING_TTL_S
        s.delete(row)
        if fresh:
            done = s.get(Credential, (ucc, user_id))
            if done is None:
                s.add(Credential(ucc=ucc, linked_user_id=user_id, password_enc=enc, created_at=now))
            else:
                done.password_enc, done.created_at = enc, now
        s.commit()
        return fresh


def delete_pending(ucc: str, user_id: str) -> int:
    with _sf()() as s:
        n = s.execute(delete(PendingCredential).where(PendingCredential.ucc == ucc, PendingCredential.linked_user_id == user_id)).rowcount
        s.commit()
        return int(n or 0)


def expire_pending(now: float | None = None) -> list[tuple[str, str]]:
    """Deletes every pending link older than PENDING_TTL_S; returns the (UCC, user id) pairs deleted."""
    before = (time.time() if now is None else now) - PENDING_TTL_S
    with _sf()() as s:
        rows = s.execute(delete(PendingCredential).where(PendingCredential.created_at < before).returning(PendingCredential.ucc, PendingCredential.linked_user_id)).all()
        s.commit()
        return [(r[0], r[1]) for r in rows]


def record_link_failure(ucc: str, now: float | None = None) -> None:
    with _sf()() as s:
        s.add(LinkFailure(ucc=ucc, at=time.time() if now is None else now))
        s.commit()


def link_failures(ucc: str, window_s: float, now: float | None = None) -> int:
    """Failed verification logins for this UCC in the rolling window (rows far older than the window are pruned)."""
    since = (time.time() if now is None else now) - window_s
    with _sf()() as s:
        s.execute(delete(LinkFailure).where(LinkFailure.at < since - window_s))
        n = s.execute(select(func.count()).select_from(LinkFailure).where(LinkFailure.ucc == ucc, LinkFailure.at >= since)).scalar_one()
        s.commit()
        return int(n or 0)


def session() -> Session:
    return _sf()()
