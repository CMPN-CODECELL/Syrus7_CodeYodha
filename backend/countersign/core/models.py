"""Relational schema for the app database. Invariants that Postgres can enforce are enforced here
(unique keys, check constraints, append-only audit trigger) rather than only in application code."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(254))  # stored lowercased; unique index on lower(email)
    password_hash: Mapped[str | None] = mapped_column(Text)  # Argon2id; demo and Attack Lab users have none
    login_failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    login_locked_until: Mapped[datetime | None] = mapped_column(TS)  # account-wide backstop lock (distributed guessing)
    login_last_failed_at: Mapped[datetime | None] = mapped_column(TS)  # failures older than the window no longer count
    webauthn_user_handle: Mapped[bytes] = mapped_column(LargeBinary)
    pin_hash: Mapped[str | None] = mapped_column(Text)
    pin_failed_count: Mapped[int] = mapped_column(Integer, default=0)
    pin_locked_until: Mapped[datetime | None] = mapped_column(TS)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_pending_enc: Mapped[str | None] = mapped_column(Text)  # a new secret being set up; the confirmed one stays active
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger)
    is_lab_user: Mapped[bool] = mapped_column(Boolean, default=False)
    active_account_id: Mapped[str | None] = mapped_column(String(32))  # the account this trader is acting on now
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class DeviceCredential(Base):
    __tablename__ = "device_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    credential_id: Mapped[str] = mapped_column(Text, unique=True)  # base64url
    public_key: Mapped[bytes] = mapped_column(LargeBinary)  # COSE key, never the private key
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)
    transports: Mapped[list] = mapped_column(JSONB, default=list)
    label: Mapped[str] = mapped_column(String(128), default="Passkey")
    aaguid: Mapped[str | None] = mapped_column(String(64))
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    is_virtual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)


class WebauthnChallenge(Base):
    __tablename__ = "webauthn_challenges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))  # NULL for a passkey sign-in (user not known yet)
    purpose: Mapped[str] = mapped_column(String(96))  # REGISTRATION:<label up to 60 chars> | LOGIN
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TS)
    used_at: Mapped[datetime | None] = mapped_column(TS)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    user_agent: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    step_up_at: Mapped[datetime | None] = mapped_column(TS)  # last fresh proof of an approval factor in this session
    step_up_method: Mapped[str | None] = mapped_column(String(16))


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(64))
    segments: Mapped[list] = mapped_column(JSONB, default=list)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    broker_kind: Mapped[str] = mapped_column(String(8), default="paper", server_default="paper")  # paper | 021
    # 021 accounts only: 'operator' (the .env account, operated by CS_021_OWNER_EMAILS) or 'self' (self-service link)
    linked_via: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (CheckConstraint("broker_kind in ('paper','021')", name="ck_account_broker_kind"),)


class AccountMember(Base):
    """Extra traders allowed to operate an account (the shared 021 sandbox account). The owner is accounts.user_id."""

    __tablename__ = "account_members"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class BrokerLink(Base):
    """A trader's self-service link to a 021 account: they proved its UCC and password through the gateway (which alone
    keeps the sealed password). It grants operating that account until they disconnect."""

    __tablename__ = "broker_links"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Instrument(Base):
    __tablename__ = "instruments"
    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), default="NSE")
    segment: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    raw_name: Mapped[str] = mapped_column(Text)  # untrusted vendor text; never sent to the model
    isin: Mapped[str | None] = mapped_column(String(16))
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    tick_size_paise: Mapped[int] = mapped_column(Integer, default=5)
    freeze_qty: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    prev_close_paise: Mapped[int] = mapped_column(BigInteger)
    lower_circuit_paise: Mapped[int] = mapped_column(BigInteger)
    upper_circuit_paise: Mapped[int] = mapped_column(BigInteger)
    underlying: Mapped[str | None] = mapped_column(String(32))
    expiry: Mapped[str | None] = mapped_column(String(10))
    strike_paise: Mapped[int | None] = mapped_column(BigInteger)
    option_type: Mapped[str | None] = mapped_column(String(2))
    aliases: Mapped[list] = mapped_column(JSONB, default=list)
    flags: Mapped[list] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(String(32), default="seed")
    token_021: Mapped[int | None] = mapped_column(Integer)  # 021 instrument token (NULL: not tradable on 021)
    exchange_021: Mapped[str | None] = mapped_column(String(8))  # 021 response exchange name, e.g. NSECM


class Instrument021(Base):
    """Compact copy of the 021 instrument file (about 1,05,000 rows), loaded daily by the gateway so any process can
    resolve symbols without a 021 credential. Prices are paise; expiry is 021's integer timestamp (Unix seconds)."""

    __tablename__ = "instruments_021"
    exchange: Mapped[str] = mapped_column(String(8), primary_key=True)
    token: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    instrument_type: Mapped[str] = mapped_column(String(8))
    lot: Mapped[int] = mapped_column(Integer)
    tick: Mapped[int] = mapped_column(Integer)
    freeze: Mapped[int] = mapped_column(Integer)
    lower_circuit: Mapped[int] = mapped_column(BigInteger)
    upper_circuit: Mapped[int] = mapped_column(BigInteger)
    expiry: Mapped[int] = mapped_column(BigInteger)
    strike: Mapped[int] = mapped_column(BigInteger)
    option_type: Mapped[str] = mapped_column(String(4))
    isin: Mapped[str] = mapped_column(String(16))
    underlying_token: Mapped[int] = mapped_column(Integer)
    underlying_exchange: Mapped[str] = mapped_column(String(8))


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[str] = mapped_column(String(64), index=True)
    ltp_paise: Mapped[int] = mapped_column(BigInteger)
    bid_paise: Mapped[int] = mapped_column(BigInteger)
    ask_paise: Mapped[int] = mapped_column(BigInteger)
    tick_ts: Mapped[datetime] = mapped_column(TS)
    seq: Mapped[int] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[str] = mapped_column(String(32))
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    canonical: Mapped[dict] = mapped_column(JSONB)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    steps_view: Mapped[list] = mapped_column(JSONB, default=list)  # fields, previews, issues per step
    issues: Mapped[list] = mapped_column(JSONB, default=list)
    interpretation: Mapped[dict] = mapped_column(JSONB, default=dict)
    engine: Mapped[str] = mapped_column(String(200))
    source_text: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32), default="chat")  # chat | attack_lab | mandate
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint(
            "status in ('BLOCKED','AWAITING_APPROVAL','APPROVED','EXECUTING','COMPLETED','PARTIALLY_COMPLETED',"
            "'FAILED','REJECTED_BY_USER','EXPIRED','ACTIVE','CANCELLED')",
            name="ck_proposal_status",
        ),
        CheckConstraint("kind in ('ORDER','PLAN','MANDATE')", name="ck_proposal_kind"),
    )


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[str] = mapped_column(String(32))
    method: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)
    credential_id: Mapped[str | None] = mapped_column(Text)
    sign_count_before: Mapped[int | None] = mapped_column(BigInteger)
    assertion: Mapped[dict | None] = mapped_column(JSONB)
    server_signature: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="CHALLENGED")
    failure_reason: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (
        CheckConstraint("status in ('CHALLENGED','VERIFIED','FAILED','EXPIRED')", name="ck_approval_status"),
        CheckConstraint("method in ('webauthn','pin','totp')", name="ck_approval_method"),
        CheckConstraint("expires_at > issued_at", name="ck_approval_window"),
        Index("uq_one_verified_approval", "proposal_id", unique=True, postgresql_where=text("status = 'VERIFIED'")),
    )


class Capability(Base):
    __tablename__ = "capabilities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), unique=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"))
    account_id: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(16))
    canonical_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(TS)
    used_at: Mapped[datetime | None] = mapped_column(TS)
    used_by_job: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (
        CheckConstraint("status in ('ACTIVE','USED','EXPIRED','REVOKED')", name="ck_cap_status"),
        CheckConstraint("(status = 'USED') = (used_at IS NOT NULL)", name="ck_cap_used_consistent"),
    )


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(32))
    plan_hash: Mapped[str] = mapped_column(String(64))
    failure_policy: Mapped[str] = mapped_column(String(32), default="STOP_ON_FAILURE")
    approved_at: Mapped[datetime | None] = mapped_column(TS)
    completed_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (
        CheckConstraint(
            "state in ('DRAFT','PREVIEWED','AWAITING_APPROVAL','APPROVED','EXECUTING','PARTIALLY_COMPLETED','COMPLETED',"
            "'PARTIALLY_FAILED','REJECTED','CANCELLED','BLOCKED')",
            name="ck_plan_state",
        ),
    )


class PlanStep(Base):
    __tablename__ = "plan_steps"
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), primary_key=True)
    step_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical: Mapped[dict] = mapped_column(JSONB)
    step_hash: Mapped[str] = mapped_column(String(64))
    depends_on: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="PENDING")
    client_order_id: Mapped[str | None] = mapped_column(String(32))
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint(
            "state in ('PENDING','SUBMITTED','OPEN','PARTIALLY_FILLED','FILLED','REJECTED','CANCELLED','FAILED','SKIPPED','BLOCKED',"
            "'NOT_PLACED','NEEDS_REVIEW')",
            name="ck_step_state",
        ),
    )


class StandingMandate(Base):
    __tablename__ = "mandates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    predicate: Mapped[dict] = mapped_column(JSONB)
    template: Mapped[dict | None] = mapped_column(JSONB)
    max_quantity: Mapped[int | None] = mapped_column(Integer)
    price_limit_paise: Mapped[int | None] = mapped_column(BigInteger)
    mandate_hash: Mapped[str] = mapped_column(String(64))
    one_shot: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_id: Mapped[str | None] = mapped_column(String(36))
    capability_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(24), default="PENDING_APPROVAL")
    expires_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(TS)
    triggered_at: Mapped[datetime | None] = mapped_column(TS)
    fired_at: Mapped[datetime | None] = mapped_column(TS)
    trigger_event_id: Mapped[str | None] = mapped_column(String(96), unique=True)
    trigger_tick: Mapped[dict | None] = mapped_column(JSONB)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(TS)
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint(
            "state in ('PENDING_APPROVAL','ACTIVE','TRIGGERED','EXECUTING','FIRED','COMPLETED','NEEDS_REVIEW','EXPIRED','CANCELLED','FAILED')",
            name="ck_mandate_state",
        ),
        CheckConstraint("kind in ('ORDER','ALERT')", name="ck_mandate_kind"),
        CheckConstraint("kind = 'ALERT' OR (max_quantity IS NOT NULL AND price_limit_paise IS NOT NULL)", name="ck_mandate_scope"),
    )


class ExecJob(Base):
    __tablename__ = "exec_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    proposal_id: Mapped[str] = mapped_column(String(36), index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(36))
    order_request: Mapped[dict | None] = mapped_column(JSONB)  # mandate fire: order generated at trigger time
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    reason: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(32), default="approval")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (
        CheckConstraint("status in ('PENDING','HELD','RUNNING','DONE','REJECTED','HALTED')", name="ck_job_status"),
    )


class OutboxEntry(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("exec_jobs.id"), index=True)
    proposal_id: Mapped[str] = mapped_column(String(36), index=True)
    capability_id: Mapped[str] = mapped_column(String(36))
    step_no: Mapped[int] = mapped_column(Integer, default=1)
    client_order_id: Mapped[str] = mapped_column(String(20), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    correlation_id: Mapped[str] = mapped_column(String(36))
    request: Mapped[dict] = mapped_column(JSONB)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="PREPARED")
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    broker_status: Mapped[str | None] = mapped_column(String(24))
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint(
            "state in ('PREPARED','SENDING','ACKED','UNKNOWN','RECONCILED_FOUND','RECONCILED_ABSENT','REJECTED_BY_BROKER','FAILED',"
            "'NOT_PLACED','NEEDS_REVIEW')",
            name="ck_outbox_state",
        ),
        UniqueConstraint("capability_id", "step_no", name="uq_outbox_capability_step"),
    )


class BrokerOrderMirror(Base):
    __tablename__ = "broker_orders"
    broker_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str | None] = mapped_column(String(20), index=True)
    account_id: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())


class OrderFill(Base):
    __tablename__ = "order_fills"
    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker_order_id: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    price_paise: Mapped[int] = mapped_column(BigInteger)
    filled_at: Mapped[datetime] = mapped_column(TS)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TS)
    actor: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class SecurityEvent(Base):
    __tablename__ = "security_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    severity: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    account_id: Mapped[str | None] = mapped_column(String(32))  # the account it concerns; NULL means system-wide


class OutcomeRow(Base):
    __tablename__ = "outcomes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16))
    proposal_id: Mapped[str | None] = mapped_column(String(36))
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(32))


class SystemState(Base):
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    cards: Mapped[list] = mapped_column(JSONB, default=list)
    engine: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class AttackRunRow(Base):
    __tablename__ = "attack_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    started_at: Mapped[datetime] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    results: Mapped[list] = mapped_column(JSONB, default=list)
    scoreboard: Mapped[dict] = mapped_column(JSONB, default=dict)


# ------------------------------------------------------------------ WhatsApp alerts (notification and read-only channel)


class WhatsAppLink(Base):
    """A trader's WhatsApp number. Messages go only to a verified, opted-in, unpaused link. WhatsApp can never approve,
    place, modify or cancel anything."""

    __tablename__ = "whatsapp_links"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(20), index=True)
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    opted_in: Mapped[bool] = mapped_column(Boolean, default=True)
    paused_until: Mapped[datetime | None] = mapped_column(TS)
    last_inbound_at: Mapped[datetime | None] = mapped_column(TS)  # a message from the trader reopens the 24-hour window
    window_closed_at: Mapped[datetime | None] = mapped_column(TS)  # last Twilio 63016 (outside the 24-hour window)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class WhatsAppCode(Base):
    """Single-use 6-digit verification code (stored hashed), valid for 10 minutes and at most 5 attempts."""

    __tablename__ = "whatsapp_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    phone_e164: Mapped[str] = mapped_column(String(20))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(TS)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class WhatsAppPrefs(Base):
    __tablename__ = "whatsapp_prefs"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    orders: Mapped[bool] = mapped_column(Boolean, default=True)
    mandates: Mapped[bool] = mapped_column(Boolean, default=True)
    security: Mapped[bool] = mapped_column(Boolean, default=True)
    digests: Mapped[bool] = mapped_column(Boolean, default=False)


class WhatsAppWatch(Base):
    __tablename__ = "whatsapp_watches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    instrument_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    interval_min: Mapped[int] = mapped_column(Integer)
    min_move_pct: Mapped[float | None] = mapped_column(Float)
    market_hours_only: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(TS)
    last_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (
        CheckConstraint("interval_min in (2,5,10,15,30)", name="ck_wa_watch_interval"),
        UniqueConstraint("user_id", "instrument_id", name="uq_wa_watch_user_instrument"),
    )


class WhatsAppMessage(Base):
    """Log of every WhatsApp message (out and in). Phones are stored masked; bodies only as a hash and a short preview."""

    __tablename__ = "whatsapp_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(TS, server_default=func.now(), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    direction: Mapped[str] = mapped_column(String(3), default="out")  # out | in
    kind: Mapped[str] = mapped_column(String(24))
    phone_masked: Mapped[str] = mapped_column(String(24))
    body_hash: Mapped[str] = mapped_column(String(64))
    preview: Mapped[str] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(24))
    twilio_sid: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[int | None] = mapped_column(Integer)
    source_ref: Mapped[str | None] = mapped_column(String(96))  # the event a notification came from (exactly once per user)
    checked_at: Mapped[datetime | None] = mapped_column(TS)  # when the delivery status was last read back from Twilio
    __table_args__ = (UniqueConstraint("user_id", "source_ref", name="uq_wa_message_source"),)


AUDIT_GUARD_SQL = """
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only (%% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_audit_append_only ON audit_events;
CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
"""


# In-place migrations for databases created before accounts existed. Each entry is applied only when its check
# finds it missing, so a restart does not take needless table locks.
MIGRATIONS = [
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='email'", "ALTER TABLE users ADD COLUMN IF NOT EXISTS email varchar(254)"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='password_hash'", "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='login_failed_count'", "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_failed_count integer NOT NULL DEFAULT 0"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='login_locked_until'", "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_locked_until timestamptz"),
    ("SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname='uq_users_email_lower'", "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (lower(email)) WHERE email IS NOT NULL"),
    (
        "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='webauthn_challenges' AND column_name='user_id' AND is_nullable='YES'",
        "ALTER TABLE webauthn_challenges ALTER COLUMN user_id DROP NOT NULL",
    ),
    (
        "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='webauthn_challenges' AND column_name='purpose' AND character_maximum_length >= 96",
        "ALTER TABLE webauthn_challenges ALTER COLUMN purpose TYPE varchar(96)",
    ),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='login_last_failed_at'", "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_last_failed_at timestamptz"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='totp_pending_enc'", "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_pending_enc text"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='sessions' AND column_name='step_up_at'", "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS step_up_at timestamptz"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='sessions' AND column_name='step_up_method'", "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS step_up_method varchar(16)"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='security_events' AND column_name='account_id'", "ALTER TABLE security_events ADD COLUMN IF NOT EXISTS account_id varchar(32)"),
    # 021 integration: broker kind per account, the trader's active account, 021 tokens on instruments, and the
    # NOT_PLACED / NEEDS_REVIEW execution states (021 orders are never retried automatically).
    (
        "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='accounts' AND column_name='broker_kind'",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS broker_kind varchar(8) NOT NULL DEFAULT 'paper'; "
        "ALTER TABLE accounts ADD CONSTRAINT ck_account_broker_kind CHECK (broker_kind in ('paper','021'))",
    ),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='active_account_id'", "ALTER TABLE users ADD COLUMN IF NOT EXISTS active_account_id varchar(32)"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='instruments' AND column_name='token_021'", "ALTER TABLE instruments ADD COLUMN IF NOT EXISTS token_021 integer"),
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='instruments' AND column_name='exchange_021'", "ALTER TABLE instruments ADD COLUMN IF NOT EXISTS exchange_021 varchar(8)"),
    (
        "SELECT 1 FROM pg_constraint WHERE conname='ck_outbox_state' AND position('NOT_PLACED' in pg_get_constraintdef(oid)) > 0",
        "ALTER TABLE outbox DROP CONSTRAINT IF EXISTS ck_outbox_state; ALTER TABLE outbox ADD CONSTRAINT ck_outbox_state CHECK (state in "
        "('PREPARED','SENDING','ACKED','UNKNOWN','RECONCILED_FOUND','RECONCILED_ABSENT','REJECTED_BY_BROKER','FAILED','NOT_PLACED','NEEDS_REVIEW'))",
    ),
    (
        "SELECT 1 FROM pg_constraint WHERE conname='ck_step_state' AND position('NOT_PLACED' in pg_get_constraintdef(oid)) > 0",
        "ALTER TABLE plan_steps DROP CONSTRAINT IF EXISTS ck_step_state; ALTER TABLE plan_steps ADD CONSTRAINT ck_step_state CHECK (state in "
        "('PENDING','SUBMITTED','OPEN','PARTIALLY_FILLED','FILLED','REJECTED','CANCELLED','FAILED','SKIPPED','BLOCKED','NOT_PLACED','NEEDS_REVIEW'))",
    ),
    ("SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname='ix_instruments_token_021'", "CREATE INDEX IF NOT EXISTS ix_instruments_token_021 ON instruments (exchange_021, token_021)"),
    # Self-service 021 linking: how a 021 account was linked (the broker_links table is new, so create_all makes it).
    ("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='accounts' AND column_name='linked_via'", "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS linked_via varchar(16)"),
]


# Older security events carry no account: attribute them from what they reference. Attack Lab refusals on the shared
# LAB1 sandbox were blocked by design, so they are resolved; references to since-deleted rows get '' (owner unknown,
# so no trader's Security Center lists them; they stay in the table and the audit chain). Idempotent: only rows with
# a NULL account are touched.
BACKFILL = [
    "UPDATE security_events e SET account_id = p.account_id FROM proposals p WHERE e.account_id IS NULL AND p.id = e.data->>'proposal_id'",
    "UPDATE security_events e SET account_id = p.account_id FROM approvals a JOIN proposals p ON p.id = a.proposal_id WHERE e.account_id IS NULL AND a.id = e.data->>'approval_id'",
    "UPDATE security_events e SET account_id = p.account_id FROM exec_jobs j JOIN proposals p ON p.id = j.proposal_id WHERE e.account_id IS NULL AND j.id = e.data->>'job_id'",
    "UPDATE security_events e SET account_id = b.account_id FROM broker_orders b WHERE e.account_id IS NULL AND b.broker_order_id = e.data->>'broker_order_id'",
    "UPDATE security_events SET account_id = 'LAB1' WHERE account_id IS NULL AND kind = 'UNAUTHORIZED_BROKER_ORDER' AND data->>'source' = 'rogue'",
    "UPDATE security_events SET account_id = '' WHERE account_id IS NULL AND (data ? 'proposal_id' OR data ? 'approval_id' OR data ? 'job_id' OR data ? 'broker_order_id')",
    "UPDATE security_events SET resolved = true WHERE account_id = 'LAB1' AND NOT resolved AND kind <> 'UNAUTHORIZED_BROKER_ORDER'",
    # 021 accounts created before self-service linking were all the operator-linked .env account.
    "UPDATE accounts SET linked_via = 'operator' WHERE broker_kind = '021' AND linked_via IS NULL",
]


def migrate(conn) -> None:
    for check, ddl in MIGRATIONS:
        if conn.exec_driver_sql(check).first() is None:
            conn.exec_driver_sql(ddl)
    for sql in BACKFILL:
        conn.exec_driver_sql(sql)


def create_all() -> None:
    from countersign.core.db import engine

    Base.metadata.create_all(engine())
    with engine().begin() as c:
        c.exec_driver_sql(AUDIT_GUARD_SQL)
        migrate(c)
