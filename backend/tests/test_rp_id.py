"""WebAuthn RP ID selection: passkeys work on localhost and on an allowed HTTPS deployment, never on unlisted hosts."""

import base64
import json

import pytest

from countersign.core.config import rp_id_for, rp_id_from_credential, settings


@pytest.fixture
def tunnel_origin(monkeypatch):
    monkeypatch.setattr(settings(), "extra_origins", "http://127.0.0.1:3000,https://countersign.example.com")


def _cred(origin: str) -> dict:
    client_data = base64.urlsafe_b64encode(json.dumps({"origin": origin}).encode()).decode().rstrip("=")
    return {"response": {"clientDataJSON": client_data}}


def test_allowed_https_origin_uses_its_hostname(tunnel_origin):
    assert rp_id_for("https://countersign.example.com") == "countersign.example.com"
    assert rp_id_for("http://localhost:3000") == "localhost"


def test_unlisted_origin_and_ip_fall_back_to_default(tunnel_origin):
    assert rp_id_for("https://evil.example") == settings().rp_id
    assert rp_id_for("http://127.0.0.1:3000") == settings().rp_id
    assert rp_id_for(None) == settings().rp_id


def test_rp_id_from_signed_client_data(tunnel_origin):
    assert rp_id_from_credential(_cred("https://countersign.example.com")) == "countersign.example.com"
    assert rp_id_from_credential(_cred("https://evil.example")) == settings().rp_id
    assert rp_id_from_credential({"response": {"clientDataJSON": "not-json"}}) == settings().rp_id
