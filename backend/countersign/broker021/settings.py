"""Settings of the gateway021 process: the ONLY place the 021 UCC, password and access token exist.

No other process constructs this class. The password is a SecretStr, so it never appears in a repr, log or error."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from countersign.core.config import REPO_ROOT


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CS_021_", env_file=(REPO_ROOT / ".env", REPO_ROOT / ".env.local"), extra="ignore")

    ucc: str = ""
    password: SecretStr = SecretStr("")
    rest_base: str = "https://devapi.021.trade/api/developer-api/v1"
    ws_base: str = "wss://devapi.021.trade/api/developer/websocket"
    port: int = 8200
    read_timeout_s: float = 8.0  # one idempotent GET
    write_timeout_s: float = 10.0  # one POST / PUT / DELETE (sent once, never retried)
    read_retries: int = 4  # extra attempts for a GET on 429 / 500 / 503 / timeout, with backoff and jitter
    reconcile_window_s: float = 8.0  # how long the order book is searched after an ambiguous placement
    clock_skew_s: float = 5.0  # tolerance between our send time and 021's order time (after measuring the offset)
    late_match_window_s: float = 900.0  # a NOT_PLACED placement is still matched if 021 shows it later
    publish_ticks: bool = True  # write ticks to Valkey (tick keys, history and the ticks channel)
    # Standalone checks next to a running stack: Valkey pub/sub channels are shared by every database, so an isolated
    # gateway writes tick keys to its own Valkey database but never publishes on a channel (ticks or order updates).
    isolated: bool = False
    tick_median: int = 5  # publish the median of the last N updates per instrument (1 = raw); see marketdata.py
    market_ws: bool = True
    orders_ws: bool = True
    test_faults: bool = False  # enables the two gateway drills on /admin/faults (fail_before_send, drop_after_accept); off by default (off by default)
    instruments_cache: str = str(REPO_ROOT / "backend" / ".cache" / "021_instruments.csv.gz")

    @field_validator("ucc", mode="before")
    @classmethod
    def _normalize_ucc(cls, v: object) -> str:
        return normalize_ucc(v)

    @property
    def configured(self) -> bool:
        return bool(self.ucc and self.password.get_secret_value())


def normalize_ucc(v: object) -> str:
    """The one spelling of a UCC used everywhere (settings, X-Account, linking, the API): trimmed and upper-case."""
    return str(v or "").strip().upper()


@lru_cache
def gateway_settings() -> GatewaySettings:
    return GatewaySettings()
