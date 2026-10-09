"""BrokerProvider interfaces.

ReadGateway: positions, holdings, funds, orders. Constructed with a READ key; the broker rejects any write.
OrderExecutionProvider: place/modify/cancel. Only the executor process can construct it (it alone holds the
WRITE key).

Two brokers speak the same /v1 interface with the same key scopes: the local paper broker (paper accounts DEMO1, LAB1,
NAIVE1, PT...) and the gateway021 process (the real 021 sandbox account; it alone holds the 021 credentials). The base
URL is chosen per account from accounts.broker_kind, so callers never need to know which broker they talk to."""

from __future__ import annotations

import ssl
import time
from functools import lru_cache
from typing import Any

import httpx

from countersign.core.config import settings


@lru_cache(maxsize=1)
def _tls() -> ssl.SSLContext:
    # Loading the CA bundle costs about 200 ms per httpx.Client on Windows; build it once per process.
    return ssl.create_default_context()


class BrokerError(Exception):
    def __init__(self, status: int, code: str, message: str, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.body = body or {}


class BrokerTimeout(Exception):
    """The request may or may not have been accepted. The caller must reconcile, never blindly retry."""


class BrokerUnavailable(Exception):
    """Connection refused before the request was sent. Safe to classify as not-submitted."""


_kinds: dict[str, str] = {}


def broker_kind(account_id: str | None) -> str:
    """'021' for the real 021 sandbox account, 'paper' otherwise (cached; accounts never change kind)."""
    if not account_id:
        return "paper"
    if account_id not in _kinds:
        kind = "paper"
        try:
            from countersign.core.db import session_scope
            from countersign.core.models import Account

            with session_scope() as s:
                acct = s.get(Account, account_id)
                if acct is None:
                    return "paper"  # not cached: the account may be created (linked) later
                kind = acct.broker_kind
        except Exception:  # noqa: BLE001  (no database in this process: paper)
            return "paper"
        _kinds[account_id] = kind
    return _kinds[account_id]


def base_url_for(account_id: str | None) -> str:
    return settings().gateway021_url if broker_kind(account_id) == "021" else settings().broker_url


class _Base:
    def __init__(self, key: str, base_url: str | None = None, timeout: float = 5.0, account_id: str | None = None):
        if not key:
            raise RuntimeError(f"{type(self).__name__} requires a broker key for this process.")
        headers = {"Authorization": f"Bearer {key}"}
        if account_id:
            headers["X-Account"] = account_id
        self.account_id = account_id
        self.kind = broker_kind(account_id)
        self._client = httpx.Client(base_url=base_url or base_url_for(account_id), headers=headers, timeout=timeout, verify=_tls())

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.ConnectError as e:
            raise BrokerUnavailable(str(e)) from e
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError) as e:
            raise BrokerTimeout(str(e)) from e
        except httpx.TimeoutException as e:
            raise BrokerTimeout(str(e)) from e
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"code": "BROKER_ERROR", "message": r.text}
            if not isinstance(body, dict):
                body = {"code": "BROKER_ERROR", "message": r.text}
            raise BrokerError(r.status_code, body.get("code", "BROKER_ERROR"), body.get("message", r.text), body)
        return r.json()

    def health(self) -> tuple[bool, int | None]:
        t = time.perf_counter()
        try:
            self._client.get("/health", timeout=2.0)
            return True, int((time.perf_counter() - t) * 1000)
        except httpx.HTTPError:
            return False, None


class ReadGateway(_Base):
    """Read-only access. Holds no order endpoints by construction."""

    def __init__(self, key: str | None = None, account_id: str | None = None, base_url: str | None = None):
        # 021 reads may be retried by the gateway (rate limits, 5xx), so they get more time than the local paper broker.
        slow = broker_kind(account_id) == "021" or (base_url or "") == settings().gateway021_url
        super().__init__(key if key is not None else settings().broker_read_key, base_url=base_url, account_id=account_id, timeout=20.0 if slow else 5.0)

    def funds(self) -> dict:
        return self._req("GET", "/v1/funds")

    def holdings(self) -> list[dict]:
        return self._req("GET", "/v1/holdings")

    def positions(self) -> list[dict]:
        return self._req("GET", "/v1/positions")

    def orders(self, status: str = "all", tag: str | None = None) -> list[dict]:
        params: dict[str, str] = {"status": status}
        if tag:
            params["tag"] = tag
        return self._req("GET", "/v1/orders", params=params)

    def order(self, broker_order_id: str) -> dict:
        return self._req("GET", f"/v1/orders/{broker_order_id}")

    def trades(self) -> list[dict]:
        return self._req("GET", "/v1/trades")

    # 021 gateway only (the paper broker has no such endpoints).

    def attempt(self, tag: str, wait_s: float = 0.0) -> dict:
        """The gateway's record of a placement by tag: state SENDING | ACCEPTED | REJECTED | NOT_PLACED | AMBIGUOUS |
        UNCONFIRMED. 404 TAG_UNKNOWN means the gateway never received it, so nothing reached 021."""
        return self._req("GET", f"/v1/attempts/{tag}", params={"wait_s": wait_s}, timeout=wait_s + 10.0)

    def account_info(self) -> dict:
        return self._req("GET", "/v1/account")

    def market_subscribe(self, instrument_ids: list[str]) -> dict:
        return self._req("POST", "/v1/market/subscribe", json={"instrument_ids": instrument_ids})

    def option_chain(self, underlying: str = "NIFTY", expiry: str | None = None, width: int = 8) -> dict:
        params: dict = {"underlying": underlying, "width": width}
        if expiry:
            params["expiry"] = expiry
        return self._req("GET", "/v1/market/option-chain", params=params, timeout=15.0)


_read_gateways: dict[str, ReadGateway] = {}
_market021: list[ReadGateway] = []


def market_gateway021() -> ReadGateway:
    """Read-key client for the 021 gateway's market endpoints (option chain, quotes): market data, no account."""
    if not _market021:
        _market021.append(ReadGateway(base_url=settings().gateway021_url))
    return _market021[0]


def read_gateway(account_id: str) -> ReadGateway:
    """One read-only client per trader account (the broker requires X-Account for a multi-account key)."""
    gw = _read_gateways.get(account_id)
    if gw is None or gw.kind != broker_kind(account_id):
        gw = _read_gateways[account_id] = ReadGateway(account_id=account_id)
    return gw


class OrderExecutionProvider(ReadGateway):
    """Write access. Only the executor may construct this (it alone is given the write key)."""

    def __init__(self, key: str | None = None, timeout: float | None = None, account_id: str | None = None):
        # The 021 gateway reconciles an ambiguous placement itself before answering (up to its reconcile window), so
        # its calls get a longer timeout than the local paper broker's.
        default = settings().executor_021_timeout_s if broker_kind(account_id) == "021" else settings().executor_broker_timeout_s
        _Base.__init__(
            self,
            key if key is not None else settings().broker_write_key,
            timeout=timeout or default,
            account_id=account_id,
        )

    def place(self, body: dict) -> dict:
        return self._req("POST", "/v1/orders", json=body)

    def modify(self, broker_order_id: str, body: dict) -> dict:
        return self._req("PUT", f"/v1/orders/{broker_order_id}", json=body)

    def cancel(self, broker_order_id: str) -> dict:
        return self._req("DELETE", f"/v1/orders/{broker_order_id}")


class BrokerAdmin(_Base):
    """Demo/presenter controls on the paper broker (fault injection, reset). Never used for trading.
    With base_url=settings().gateway021_url it reads the 021 gateway's order book (admin / audit scope)."""

    def __init__(self, key: str | None = None, base_url: str | None = None):
        super().__init__(key if key is not None else settings().broker_admin_key, base_url=base_url or settings().broker_url)

    def set_fault(self, mode: str, count: int = 1) -> dict:
        return self._req("POST", "/admin/faults", json={"mode": mode, "count": count})

    def fault_state(self) -> dict:
        return self._req("GET", "/admin/faults")

    def reset(self) -> dict:
        return self._req("POST", "/admin/reset")

    def inject_rogue_order(self, account_id: str, body: dict) -> dict:
        return self._req("POST", "/admin/rogue-order", json={"account_id": account_id, **body})

    def all_orders(self) -> list[dict]:
        return self._req("GET", "/admin/orders")

    def replenish(self, account_id: str) -> dict:
        return self._req("POST", f"/admin/replenish/{account_id}")

    def create_account(self, account_id: str) -> dict:
        """Provisions a new paper trading account seeded like DEMO1 (idempotent)."""
        return self._req("POST", "/admin/accounts", json={"account_id": account_id})

    def reset_account(self, account_id: str) -> dict:
        """Hard reset of one trader account: deletes its orders and trades and reseeds cash, holdings, position."""
        return self._req("POST", f"/admin/accounts/{account_id}/reset")


class NaiveSandbox(_Base):
    """The intentionally unsafe baseline copilot's direct order tool, scoped by the broker to NAIVE1 only."""

    def __init__(self, key: str | None = None):
        super().__init__(key if key is not None else settings().broker_naive_key, timeout=1.0)

    def place(self, body: dict) -> dict:
        return self._req("POST", "/v1/orders", json=body)

    def orders(self) -> list[dict]:
        return self._req("GET", "/v1/orders", params={"status": "all"})
