"""Credential vault of the gateway021 process for self-service 021 linking.

Two keys, generated once under the gitignored .secrets/ folder and read only by gateway021 (by convention: process
separation on one machine, not an HSM; any process running as the same OS user could read the files):
  - gateway021_link_key.pem: RSA-2048. The browser encrypts {ucc, password, nonce, user_id} to its public key with
    WebCrypto RSA-OAEP SHA-256, so the API only ever relays ciphertext it cannot read.
  - gateway021_store.key: a 256-bit AES-GCM data key. A linked account's password is stored sealed with it (the UCC
    and the linking user id are bound as associated data), so the gateway can log in again after 021 ends every
    session at 05:00 IST. Nothing here is ever logged or returned."""

from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from countersign.core.config import REPO_ROOT

LINK_KEY_FILE = "gateway021_link_key.pem"
STORE_KEY_FILE = "gateway021_store.key"
OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
MAX_PLAINTEXT = 190  # RSA-2048 with OAEP SHA-256: 256 - 2 * 32 - 2 bytes


class VaultError(Exception):
    """The ciphertext could not be opened (wrong key, tampered or malformed). Never carries plaintext."""


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # another start created it first; the existing key wins
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class Vault:
    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else REPO_ROOT / ".secrets"
        self._lock = threading.Lock()
        self._priv: rsa.RSAPrivateKey | None = None
        self._aead: AESGCM | None = None

    # ------------------------------------------------------------------ link key (browser -> gateway)

    def _private_key(self) -> rsa.RSAPrivateKey:
        with self._lock:
            if self._priv is None:
                p = self.dir / LINK_KEY_FILE
                if not p.exists():
                    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                    _write_once(p, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                loaded = serialization.load_pem_private_key(p.read_bytes(), password=None)
                if not isinstance(loaded, rsa.RSAPrivateKey) or loaded.key_size != 2048:
                    raise VaultError(f"{LINK_KEY_FILE} is not an RSA-2048 private key.")
                self._priv = loaded
            return self._priv

    def public_key_spki_b64(self) -> str:
        der = self._private_key().public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return base64.b64encode(der).decode()

    def open_link(self, ciphertext_b64: str) -> dict:
        """Decrypts the browser's RSA-OAEP ciphertext into its JSON object. Any failure is one opaque VaultError."""
        try:
            ct = base64.b64decode(ciphertext_b64, validate=True)
            pt = self._private_key().decrypt(ct, OAEP)
            data = json.loads(pt.decode("utf-8"))
        except (ValueError, binascii.Error, UnicodeDecodeError, TypeError):
            raise VaultError("The encrypted credentials could not be opened.") from None
        if not isinstance(data, dict):
            raise VaultError("The encrypted credentials could not be opened.")
        return data

    # ------------------------------------------------------------------ credentials at rest

    def _store_key(self) -> AESGCM:
        with self._lock:
            if self._aead is None:
                p = self.dir / STORE_KEY_FILE
                if not p.exists():
                    _write_once(p, base64.b64encode(AESGCM.generate_key(bit_length=256)))
                key = base64.b64decode(p.read_bytes().strip())
                if len(key) != 32:
                    raise VaultError(f"{STORE_KEY_FILE} is not a 256-bit key.")
                self._aead = AESGCM(key)
            return self._aead

    def ensure_keys(self) -> None:
        """Creates both keys on first start (no-op afterwards)."""
        self._private_key()
        self._store_key()

    @staticmethod
    def _aad(ucc: str, user_id: str) -> bytes:
        return f"countersign-g021-credential|{ucc}|{user_id}".encode()

    def seal(self, password: str, ucc: str, user_id: str) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + self._store_key().encrypt(nonce, password.encode("utf-8"), self._aad(ucc, user_id))).decode()

    def unseal(self, blob: str, ucc: str, user_id: str) -> str:
        try:
            raw = base64.b64decode(blob, validate=True)
            return self._store_key().decrypt(raw[:12], raw[12:], self._aad(ucc, user_id)).decode("utf-8")
        except (ValueError, binascii.Error, InvalidTag, UnicodeDecodeError):
            raise VaultError("A stored 021 credential could not be opened with this gateway's key.") from None


@lru_cache
def vault() -> Vault:
    return Vault()
