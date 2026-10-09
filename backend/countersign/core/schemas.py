"""Typed schemas. Intents are untrusted (they come from the AI or the parser); canonical objects are
produced only by the deterministic builder/verifier and are the only things that can be signed."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

ALL_HOLDINGS_ID = "PORTFOLIO:HOLDINGS"


def _omit_defaults(data: dict, defaults: dict) -> dict:
    """Optional fields added after schema v1 are left out of the canonical JSON while they hold their default, so the
    canonical bytes (and hashes) of objects that do not use them are exactly what they were before."""
    for key, default in defaults.items():
        if key in data and data[key] == default:
            data.pop(key)
    return data


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    SL = "SL"
    SLM = "SL-M"


class Product(StrEnum):
    CNC = "CNC"
    MIS = "MIS"
    NRML = "NRML"


class Validity(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class Source(StrEnum):
    TRADER = "trader_instruction"
    INSTRUMENT = "instrument_metadata"
    FEED = "live_market_feed"
    RISK = "risk_engine"
    ACCOUNT = "account"
    DEFAULT = "system_default"
    MODEL = "model_inferred"


# ---------------------------------------------------------------- untrusted intents


class OrderIntent(BaseModel):
    """What the AI (or the deterministic parser) thinks the trader asked for. Never trusted."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["PLACE", "MODIFY", "CANCEL"] = "PLACE"
    side: Side | None = None
    instrument_ref: str | None = Field(default=None, description="Trading symbol or instrument_id")
    quantity: int | None = None
    order_type: OrderType | None = None
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    product: Product | None = None
    validity: Validity | None = None
    target_order_ref: str | None = Field(default=None, description="Broker order id for MODIFY/CANCEL")
    quantity_fraction: str | None = Field(default=None, description="Fraction of the trader's holding to sell, e.g. 1/2, 1/3, 25/100, 1/1")


class PlanIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[OrderIntent]
    sequential: bool = True


class MandateIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ORDER", "ALERT"]
    instrument_ref: str | None = None
    field: Literal["LTP", "DROP_FROM_AVG_BPS", "DAY_CHANGE_BPS"] = "LTP"
    op: Literal["LTE", "GTE"] = "LTE"
    threshold_price: Decimal | None = None
    drop_pct: Decimal | None = None
    order: OrderIntent | None = None
    expires_days: int = 7
    scope: Literal["INSTRUMENT", "ALL_HOLDINGS"] = "INSTRUMENT"


# ---------------------------------------------------------------- canonical (signable) objects


class InstrumentHandle(BaseModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    exchange: Literal["NSE"] = "NSE"
    segment: Literal["EQ", "FO", "IDX"]
    symbol: str
    display_name: str
    isin: str | None = None
    expiry: str | None = None
    strike_paise: int | None = None
    option_type: Literal["CE", "PE"] | None = None
    lot_size: int = 1
    tick_size_paise: int = 5
    flags: list[str] = Field(default_factory=list)


class OrderStateRef(BaseModel):
    """The state of an open order as the trader saw it when a MODIFY was drafted (old values, shown as old -> new)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    quantity: int
    order_type: OrderType
    limit_price_paise: int | None = None
    trigger_price_paise: int | None = None


class CanonicalOrder(BaseModel):
    """For MODIFY, every field is the FULL new state of the order (quantity, type, product, validity, limit and
    trigger), so a broker that needs the complete order on modify (021 PUT /orders/{id}) can be sent it as signed."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    action: Literal["PLACE", "MODIFY", "CANCEL"]
    account_id: str
    instrument: InstrumentHandle
    side: Side
    quantity: int
    order_type: OrderType
    product: Product
    validity: Validity
    limit_price_paise: int | None = None
    trigger_price_paise: int | None = None
    protection_price_paise: int | None = None
    target_broker_order_id: str | None = None
    modify_from: OrderStateRef | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        return _omit_defaults(handler(self), {"modify_from": None})

    def worst_case_price(self) -> int | None:
        if self.order_type in (OrderType.MARKET, OrderType.SLM):
            return self.protection_price_paise
        return self.limit_price_paise


class PlanStepSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    step_no: int
    order: CanonicalOrder
    depends_on: int | None = None


class CanonicalPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    account_id: str
    steps: list[PlanStepSpec]
    failure_policy: Literal["STOP_ON_FAILURE"] = "STOP_ON_FAILURE"


class MandatePredicate(BaseModel):
    """LTP: threshold in paise. DROP_FROM_AVG_BPS: fall below the average price, in bps (GTE). DAY_CHANGE_BPS: change
    from the previous close, in signed bps (LTE -300 means down 3% or more today)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    field: Literal["LTP", "DROP_FROM_AVG_BPS", "DAY_CHANGE_BPS"]
    op: Literal["LTE", "GTE"]
    threshold: int


class CanonicalMandate(BaseModel):
    """scope ALL_HOLDINGS watches every stock the trader holds when each tick arrives (alerts only). Its fire_policy
    ONCE_PER_HOLDING_PER_DAY is enforced by a durable dedupe key (mandate, instrument, IST trading day)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    account_id: str
    kind: Literal["ORDER", "ALERT"]
    instrument: InstrumentHandle
    predicate: MandatePredicate
    template: CanonicalOrder | None = None
    max_quantity: int | None = None
    price_limit_paise: int | None = None
    one_shot: bool = True
    expires_at: str
    gap_policy: Literal["REVIEW"] = "REVIEW"
    scope: Literal["INSTRUMENT", "ALL_HOLDINGS"] = "INSTRUMENT"
    fire_policy: Literal["ONCE", "ONCE_PER_HOLDING_PER_DAY"] = "ONCE"

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        return _omit_defaults(handler(self), {"scope": "INSTRUMENT", "fire_policy": "ONCE"})


class PriceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    instrument_id: str
    reference_price_paise: int
    band_low_paise: int
    band_high_paise: int


class ApprovalPayload(BaseModel):
    """The exact object the trader countersigns. sha256(RFC 8785(payload)) is the WebAuthn challenge."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    purpose: Literal["ORDER", "PLAN", "MANDATE"]
    proposal_id: str
    canonical_hash: str
    account_id: str
    user_id: str
    credential_ref: str
    price_refs: list[PriceRef]
    nonce: str
    issued_at: str
    expires_at: str
    mandate_id: str | None = None


# ---------------------------------------------------------------- verification output


class Issue(BaseModel):
    code: str
    severity: Literal["BLOCK", "WARN", "INFO"]
    message: str
    field: str | None = None


class FieldRow(BaseModel):
    key: str
    label: str
    display: str
    source: Source
    flagged: bool = False
    note: str | None = None


class OrderPreview(BaseModel):
    reference_price_paise: int
    band_low_paise: int
    band_high_paise: int
    band_bps: int
    worst_case_price_paise: int
    order_value_paise: int
    estimated_margin_paise: int
    estimated_charges_paise: int
    charges_breakdown: list[dict]
    current_position_qty: int
    resulting_position_qty: int
    available_margin_paise: int
    margin_after_paise: int
    exposure_after_paise: int
    tick_age_ms: int
    session: dict


class Disagreement(BaseModel):
    field: str
    trader_value: str
    model_value: str


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")
