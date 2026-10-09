"""A software WebAuthn authenticator for the Attack Lab's automated scenarios.

It produces real ES256 assertions that go through exactly the same verification code as a human passkey, so the
attacks test the genuine authorization boundary. It is only ever registered to the Attack Lab user, is flagged
`is_virtual`, and is never used for the human demo account."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cbor2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from countersign.core.approval_crypto import b64url
from countersign.core.config import REPO_ROOT, settings

KEY_PATH = REPO_ROOT / ".secrets" / "attack_lab_authenticator.pem"
CRED_ID = b"countersign-attack-lab-virtual-authenticator-v1"


class VirtualAuthenticator:
    def __init__(self) -> None:
        if KEY_PATH.exists():
            self.key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
        else:
            KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.key = ec.generate_private_key(ec.SECP256R1())
            KEY_PATH.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.sign_count = 0

    @property
    def credential_id(self) -> str:
        return b64url(CRED_ID)

    def cose_public_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def assert_challenge(self, challenge: bytes, *, origin: str | None = None, sign_count: int | None = None) -> dict:
        cfg = settings()
        self.sign_count = (sign_count if sign_count is not None else self.sign_count) + 1
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": b64url(challenge), "origin": origin or cfg.origin, "crossOrigin": False},
            separators=(",", ":"),
        ).encode()
        auth_data = hashlib.sha256(cfg.rp_id.encode()).digest() + bytes([0x05]) + self.sign_count.to_bytes(4, "big")
        sig = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": self.credential_id,
            "rawId": self.credential_id,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "authenticatorData": b64url(auth_data),
                "clientDataJSON": b64url(client_data),
                "signature": b64url(sig),
            },
        }


_VA: VirtualAuthenticator | None = None


def get() -> VirtualAuthenticator:
    global _VA
    if _VA is None:
        _VA = VirtualAuthenticator()
    return _VA


def ensure_registered(s, user_id: str) -> str:
    from sqlalchemy import select

    from countersign.core.models import DeviceCredential

    va = get()
    row = s.execute(select(DeviceCredential).where(DeviceCredential.credential_id == va.credential_id)).scalar_one_or_none()
    if row is None:
        s.add(DeviceCredential(user_id=user_id, credential_id=va.credential_id, public_key=va.cose_public_key(), sign_count=0, transports=["internal"], label="Attack Lab virtual authenticator", is_virtual=True))
        s.flush()
    return va.credential_id
