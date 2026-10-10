"""Settings of the notifier process: the ONLY place the Twilio account SID, auth token and WhatsApp sender exist.

No other process constructs this class (scripts/dev.py strips CS_TWILIO_* from every other process). The token is a
SecretStr, so it never appears in a repr, log or error. Without the SID and the token the notifier runs in log-only mode;
without a valid sender it uses the public Twilio sandbox number."""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from countersign.core.config import REPO_ROOT


class TwilioSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CS_TWILIO_", env_file=(REPO_ROOT / ".env", REPO_ROOT / ".env.local"), extra="ignore")

    account_sid: str = ""
    auth_token: SecretStr = SecretStr("")
    whatsapp_from: str = ""  # whatsapp:+14155238886 for the sandbox (the whatsapp: prefix is added when missing)
    api_base: str = "https://api.twilio.com"
    port: int = 8300
    timeout_s: float = 10.0

    @property
    def configured(self) -> bool:
        return bool(self.account_sid.strip() and self.auth_token.get_secret_value().strip())

    @property
    def sender_valid(self) -> bool:
        """True when CS_TWILIO_WHATSAPP_FROM is a phone number (with or without the whatsapp: prefix)."""
        return bool(re.fullmatch(r"\+[1-9]\d{7,14}", self.whatsapp_from.strip().removeprefix("whatsapp:").strip()))

    @property
    def sender(self) -> str:
        """The configured sender, or the public Twilio sandbox number when it is missing or not a phone number."""
        if not self.sender_valid:
            return "whatsapp:" + SANDBOX_SENDER
        return "whatsapp:" + self.whatsapp_from.strip().removeprefix("whatsapp:").strip()


SANDBOX_SENDER = "+14155238886"


@lru_cache
def twilio_settings() -> TwilioSettings:
    return TwilioSettings()
