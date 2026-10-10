import base64
import ipaddress
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CS_",
        env_file=(REPO_ROOT / ".env", REPO_ROOT / ".env.local"),
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://countersign:countersign_local@127.0.0.1:55432/countersign"
    broker_database_url: str = "postgresql+psycopg://countersign:countersign_local@127.0.0.1:55432/paperbroker"
    dbos_database_url: str = "postgresql://countersign:countersign_local@127.0.0.1:55432/countersign_dbos"
    valkey_url: str = "redis://127.0.0.1:56379/0"

    broker_url: str = "http://127.0.0.1:8100"
    # Credential zone: each process receives only the keys it is allowed to hold (see scripts/dev.py).
    broker_read_key: str = ""
    broker_write_key: str = ""
    broker_admin_key: str = ""
    broker_naive_key: str = ""
    broker_audit_key: str = ""

    account_id: str = "DEMO1"
    naive_account_id: str = "NAIVE1"

    rp_id: str = "localhost"
    rp_name: str = "Countersign"
    origin: str = "http://localhost:3000"
    extra_origins: str = "http://127.0.0.1:3000"

    approval_ttl_seconds: int = 60
    price_band_bps: int = 100
    market_protection_bps: int = 50
    tick_stale_seconds: float = 5.0
    mandate_gap_seconds: float = 30.0
    session_ttl_minutes: int = 720
    pin_max_attempts: int = 5
    executor_broker_timeout_s: float = 2.0
    reconcile_window_s: float = 6.0

    approval_signing_key_path: str = str(REPO_ROOT / ".secrets" / "approval_ed25519.pem")
    approval_public_key_path: str = str(REPO_ROOT / ".secrets" / "approval_ed25519.pub.pem")
    audit_checkpoint_key_path: str = str(REPO_ROOT / ".secrets" / "audit_ed25519.pem")
    totp_encryption_key: str = ""

    demo_mode: bool = True
    market_always_open: bool = True

    llm_provider: str = "auto"
    llm_order: str = "groq,gemini,ollama"
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    llm_timeout_s: float = 45.0

    # 021 sandbox. The UCC, password and access token live ONLY in the gateway021 process (broker021/settings.py);
    # every other process reaches 021 through the gateway's /v1 interface with its usual broker key.
    gateway021_enabled: bool = False  # set by scripts/dev.py when the gateway is started
    gateway021_url: str = "http://127.0.0.1:8200"
    owner_emails_021: str = Field("", validation_alias=AliasChoices("CS_021_OWNER_EMAILS"))
    strict_monitor_021: bool = Field(False, validation_alias=AliasChoices("CS_021_STRICT_MONITOR"))
    executor_021_timeout_s: float = 45.0
    # 021's sandbox fills about half of any order above one share and holds the rest, so orders on a 021 account are
    # signed once and sent as one-share orders (never more in total than signed). Off: send each order whole.
    slice_021: bool = Field(True, validation_alias=AliasChoices("CS_021_SLICE"))
    funds_021_daily_paise: int = 10_00_000_00  # virtual funds 021 tops up every trading day (no funds API exists)

    # WhatsApp alerts. The Twilio account SID, auth token and sender live ONLY in the notifier process
    # (notifier/settings.py); every other process reaches it over loopback with the shared notifier key.
    notifier_url: str = "http://127.0.0.1:8300"
    notifier_key: str = ""  # shared secret between the API and the notifier (scripts/dev.py generates it)
    whatsapp_public_url: str = "https://countersign.everyrepo.me/api/whatsapp/inbound"  # the URL Twilio signs
    whatsapp_join_code: str = ""  # optional: the sandbox join code ("join <code>"), shown to traders; not a secret

    @property
    def owners_021(self) -> set[str]:
        return {e.strip().lower() for e in self.owner_emails_021.split(",") if e.strip()}

    @property
    def allowed_origins(self) -> list[str]:
        extra = [o.strip() for o in self.extra_origins.split(",") if o.strip()]
        return [self.origin, *extra]


def rp_id_for(origin: str | None) -> str:
    """WebAuthn RP ID for a page origin: its hostname when that origin is allowed (so passkeys work on localhost and on
    the HTTPS deployment), otherwise the configured default. IP addresses can never be RP IDs."""
    cfg = settings()
    if origin and origin in cfg.allowed_origins:
        host = urlparse(origin).hostname or ""
        if host and not ipaddress_like(host):
            return host
    return cfg.rp_id


def rp_id_from_credential(credential: dict) -> str:
    """RP ID to verify a WebAuthn response against, taken from the origin the browser signed into clientDataJSON."""
    try:
        raw = credential["response"]["clientDataJSON"]
        return rp_id_for(json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))).get("origin"))
    except Exception:  # noqa: BLE001
        return settings().rp_id


def ipaddress_like(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


@lru_cache
def settings() -> Settings:
    return Settings()
