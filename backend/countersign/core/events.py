"""Valkey event bus. Ticks, broker order updates and app events. Not on any safety-critical path:
every safety decision re-reads Postgres or the broker, never only the bus."""

import json
from functools import lru_cache
from typing import Any

import redis

from countersign.core.config import settings

CH_TICKS = "cs:ticks"
CH_EVENTS = "cs:events"
CH_BROKER_UPDATES = "cs:broker.updates"
CH_FEED_CONTROL = "cs:feed.control"
CH_EXEC_WAKE = "cs:executor.wake"
TICK_KEY = "cs:tick:{}"
HISTORY_KEY = "cs:history:{}"


@lru_cache
def client() -> redis.Redis:
    return redis.Redis.from_url(settings().valkey_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=3)


def publish(channel: str, payload: dict[str, Any]) -> None:
    try:
        client().publish(channel, json.dumps(payload, default=str))
    except redis.RedisError:
        pass


def emit(event_type: str, data: dict[str, Any]) -> None:
    publish(CH_EVENTS, {"type": event_type, "data": data})


def ping() -> bool:
    try:
        return bool(client().ping())
    except redis.RedisError:
        return False


# Owner-scoped event types: the live stream delivers these only to the user/account they belong to.
OWNED_TYPES = {"proposal", "mandate", "order", "plan", "outcome"}


def emit_proposal(p: Any) -> None:
    emit("proposal", {"id": p.id, "status": p.status, "user_id": p.user_id, "account_id": p.account_id})


def emit_mandate(m: Any, state: str | None = None) -> None:
    emit("mandate", {"mandate_id": m.id, "state": state or m.state, "user_id": m.user_id, "account_id": m.account_id})


def visible_to(event_type: str, data: dict[str, Any], user_id: str, account_id: str) -> bool:
    """Ticks, market, monitor, health and security stay global; owned events must name this user or account."""
    if event_type not in OWNED_TYPES:
        return True
    uid = data.get("user_id")
    acct = data.get("account_id")
    if uid is None and acct is None:
        return False
    if uid is not None and uid != user_id:
        return False
    return acct is None or acct in (account_id, "*")
