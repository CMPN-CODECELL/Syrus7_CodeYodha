"""Countersignature cryptography, shared by the approval service (API) and the executor.

WebAuthn path (device-signed): challenge = sha256(RFC 8785(ApprovalPayload)). The platform authenticator
(Windows Hello, Touch ID/Face ID, Android biometrics) signs clientDataJSON containing that challenge. The executor
re-verifies the stored assertion against the credential public key and a payload it recomputes from the
database, so a tampered order or payload can never verify.

PIN/TOTP fallback (server-attested): the approval service signs the payload hash with an Ed25519 key that only
the API holds; the executor verifies with the public key. Lower assurance, labelled as such in the UI."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from webauthn import verify_authentication_response

from countersign.core.canonical import jcs, sha256_bytes, sha256_hex
from countersign.core.config import rp_id_for, settings
from countersign.core.schemas import ApprovalPayload


class CountersignError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def payload_hash(payload: ApprovalPayload | dict) -> str:
    return sha256_hex(jcs(payload))


def challenge_bytes(payload: ApprovalPayload | dict) -> bytes:
    return sha256_bytes(jcs(payload))


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_webauthn(
    assertion: dict,
    payload: ApprovalPayload | dict,
    *,
    credential_public_key: bytes,
    sign_count_before: int,
    expected_credential_id: str,
) -> int:
    """Returns the authenticator's new sign count. Raises CountersignError on any mismatch."""
    cfg = settings()
    if assertion.get("id") != expected_credential_id and assertion.get("rawId") != expected_credential_id:
        raise CountersignError("SIGNATURE_INVALID", "The passkey used is not the device bound to this approval.")
    try:
        client_data = json.loads(b64url_decode(assertion["response"]["clientDataJSON"]))
    except Exception as e:  # noqa: BLE001
        raise CountersignError("SIGNATURE_INVALID", "Malformed authenticator response.") from e
    if client_data.get("challenge") != b64url(challenge_bytes(payload)):
        raise CountersignError(
            "PAYLOAD_MISMATCH",
            "The signature was made over a different order or payload. The exact canonical order you saw is the only thing that can be approved.",
        )
    try:
        verified = verify_authentication_response(
            credential=assertion,
            expected_challenge=challenge_bytes(payload),
            expected_rp_id=rp_id_for(client_data.get("origin")),
            expected_origin=cfg.allowed_origins,
            credential_public_key=credential_public_key,
            credential_current_sign_count=sign_count_before,
            require_user_verification=True,
        )
    except Exception as e:  # noqa: BLE001
        raise CountersignError("SIGNATURE_INVALID", f"Authentication could not be verified: {e}") from e
    return verified.new_sign_count


def _ensure_keys() -> None:
    cfg = settings()
    priv = Path(cfg.approval_signing_key_path)
    pub = Path(cfg.approval_public_key_path)
    if priv.exists() and pub.exists():
        return
    priv.parent.mkdir(parents=True, exist_ok=True)
    k = Ed25519PrivateKey.generate()
    priv.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pub.write_bytes(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def server_attest(payload_hash_hex: str, method: str) -> str:
    """API only: sign the payload hash after a successful PIN/TOTP check."""
    _ensure_keys()
    k = serialization.load_pem_private_key(Path(settings().approval_signing_key_path).read_bytes(), password=None)
    assert isinstance(k, Ed25519PrivateKey)
    return b64url(k.sign(f"countersign:{method}:{payload_hash_hex}".encode()))


def verify_server_attestation(payload_hash_hex: str, method: str, signature: str) -> None:
    """Executor: verify with the public key only."""
    _ensure_keys()
    pk = serialization.load_pem_public_key(Path(settings().approval_public_key_path).read_bytes())
    assert isinstance(pk, Ed25519PublicKey)
    try:
        pk.verify(b64url_decode(signature), f"countersign:{method}:{payload_hash_hex}".encode())
    except Exception as e:  # noqa: BLE001
        raise CountersignError("SIGNATURE_INVALID", "Server attestation signature does not match this payload.") from e
