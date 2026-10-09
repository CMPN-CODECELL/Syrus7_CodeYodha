"""RFC 8785 canonical JSON and hashing. Everything that is signed goes through here."""

import hashlib
from typing import Any

import rfc8785
from pydantic import BaseModel


def to_plain(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return obj


def jcs(obj: Any) -> bytes:
    return rfc8785.dumps(to_plain(obj))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def canonical_hash(obj: Any) -> str:
    return sha256_hex(jcs(obj))


def short(h: str) -> str:
    return h[:10].upper()
