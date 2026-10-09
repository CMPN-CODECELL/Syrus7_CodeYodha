"""BrokerProvider interfaces.

ReadGateway: positions, holdings, funds, orders. Constructed with a READ key; the broker rejects any write.
OrderExecutionProvider: place/modify/cancel. Only the executor process can construct it (it alone holds the
WRITE key). A real 021 adapter implements the same two interfaces against the 021 developer APIs."""

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
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class BrokerTimeout(Exception):
    """The request may or may not have been accepted. The caller must reconcile, never blindly retry."""


class BrokerUnavailable(Exception):
    """Connection refused before the request was sent. Safe to classify as not-submitted."""


class _Base:
    def __init__(self, key: str, base_url: str | None = None, timeout: float = 5.0, account_id: str | None = None):
        if not key:
            raise RuntimeError(f"{type(self).__name__} requires a broker key for this process.")
        headers = {"Authorization": f"Bearer {key}"}
        if account_id:
            headers["X-Account"] = account_id
        self.account_id = account_id
        self._client = httpx.Client(base_url=base_url or settings().broker_url, headers=headers, timeout=timeout, verify=_tls())

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
            raise BrokerError(r.status_code, body.get("code", "BROKER_ERROR"), body.get("message", r.text))
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

    def __init__(self, key: str | None = None, account_id: str | None = None):
        super().__init__(key if key is not None else settings().broker_read_key, account_id=account_id)

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


_read_gateways: dict[str, ReadGateway] = {}


def read_gateway(account_id: str) -> ReadGateway:
    """One read-only client per trader account (the broker requires X-Account for a multi-account key)."""
    if account_id not in _read_gateways:
        _read_gateways[account_id] = ReadGateway(account_id=account_id)
    return _read_gateways[account_id]


class OrderExecutionProvider(ReadGateway):
    """Write access. Only the executor may construct this (it alone is given the write key)."""

    def __init__(self, key: str | None = None, timeout: float | None = None, account_id: str | None = None):
        _Base.__init__(
            self,
            key if key is not None else settings().broker_write_key,
            timeout=timeout or settings().executor_broker_timeout_s,
            account_id=account_id,
        )

    def place(self, body: dict) -> dict:
        return self._req("POST", "/v1/orders", json=body)

    def modify(self, broker_order_id: str, body: dict) -> dict:
        return self._req("PUT", f"/v1/orders/{broker_order_id}", json=body)

    def cancel(self, broker_order_id: str) -> dict:
        return self._req("DELETE", f"/v1/orders/{broker_order_id}")


class BrokerAdmin(_Base):
    """Demo/presenter controls on the paper broker (fault injection, reset). Never used for trading."""

    def __init__(self, key: str | None = None):
        super().__init__(key if key is not None else settings().broker_admin_key)

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
