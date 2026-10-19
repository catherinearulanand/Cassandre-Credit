"""Cryptography for the platform.

- Field-level encryption: AES-256-GCM with a key id prefix, so keys rotate without
  re-encrypting history (old key ids stay decryptable; new writes use the active key).
  Associated data binds a ciphertext to its record and field, so a ciphertext copied into
  another row fails to decrypt.
- Tokenisation: keyed HMAC-SHA256 gives stable, non-reversible tokens for joins and logs.
- Signing: Ed25519 signatures over canonical content hashes (CTS publication, approvals,
  evidence). In production the private key never leaves the HSM; ``KeyProvider`` is the seam.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Protocol

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from platform_core.errors import IntegrityError


class KeyProvider(Protocol):
    def data_key(self, key_id: str) -> bytes: ...

    def active_data_key_id(self) -> str: ...

    def tokenisation_key(self) -> bytes: ...

    def signing_key(self, key_id: str) -> Ed25519PrivateKey: ...

    def verify_key(self, key_id: str) -> Ed25519PublicKey: ...

    def active_signing_key_id(self) -> str: ...


@dataclass
class LocalDevKeyProvider:
    """Development-only key provider. Production uses an HSM-backed implementation."""

    data_keys: dict[str, bytes] = field(default_factory=dict)
    active_key: str = "dek-dev-1"
    token_key: bytes = field(default_factory=lambda: os.urandom(32))
    signing_keys: dict[str, Ed25519PrivateKey] = field(default_factory=dict)
    active_signer: str = "sig-dev-1"

    def __post_init__(self) -> None:
        self.data_keys.setdefault(self.active_key, AESGCM.generate_key(bit_length=256))
        self.signing_keys.setdefault(self.active_signer, Ed25519PrivateKey.generate())

    def rotate_data_key(self, new_id: str) -> None:
        self.data_keys[new_id] = AESGCM.generate_key(bit_length=256)
        self.active_key = new_id

    def data_key(self, key_id: str) -> bytes:
        try:
            return self.data_keys[key_id]
        except KeyError as exc:
            raise IntegrityError(f"unknown data key {key_id}") from exc

    def active_data_key_id(self) -> str:
        return self.active_key

    def tokenisation_key(self) -> bytes:
        return self.token_key

    def signing_key(self, key_id: str) -> Ed25519PrivateKey:
        return self.signing_keys[key_id]

    def verify_key(self, key_id: str) -> Ed25519PublicKey:
        try:
            return self.signing_keys[key_id].public_key()
        except KeyError as exc:
            raise IntegrityError(f"unknown signing key {key_id}") from exc

    def active_signing_key_id(self) -> str:
        return self.active_signer


class FieldCipher:
    PREFIX = "enc:v1"

    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    def encrypt(self, plaintext: str, *, context: str) -> str:
        key_id = self._keys.active_data_key_id()
        nonce = os.urandom(12)
        ct = AESGCM(self._keys.data_key(key_id)).encrypt(
            nonce, plaintext.encode("utf-8"), context.encode("utf-8")
        )
        blob = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
        return f"{self.PREFIX}:{key_id}:{blob}"

    def decrypt(self, token: str, *, context: str) -> str:
        try:
            prefix, version, key_id, blob = token.split(":", 3)
        except ValueError as exc:
            raise IntegrityError("malformed ciphertext") from exc
        if f"{prefix}:{version}" != self.PREFIX:
            raise IntegrityError("unsupported ciphertext version")
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        try:
            pt = AESGCM(self._keys.data_key(key_id)).decrypt(
                raw[:12], raw[12:], context.encode("utf-8")
            )
        except InvalidTag as exc:
            raise IntegrityError(
                "ciphertext failed authentication (tampered or wrong context)"
            ) from exc
        return pt.decode("utf-8")


class Tokeniser:
    """Deterministic keyed tokens: same input, same token; not reversible without a vault."""

    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    def token(self, kind: str, value: str) -> str:
        mac = hmac.new(self._keys.tokenisation_key(), f"{kind}|{value}".encode(), hashlib.sha256)
        return f"tok_{kind}_{mac.hexdigest()[:20]}"


@dataclass(frozen=True)
class Signature:
    key_id: str
    signature_b64: str
    signed_hash: str


class Signer:
    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    def sign_hash(self, content_hash_hex: str) -> Signature:
        key_id = self._keys.active_signing_key_id()
        sig = self._keys.signing_key(key_id).sign(bytes.fromhex(content_hash_hex))
        return Signature(key_id, base64.b64encode(sig).decode("ascii"), content_hash_hex)

    def verify(self, signature: Signature, content_hash_hex: str) -> None:
        if signature.signed_hash != content_hash_hex:
            raise IntegrityError("content hash differs from signed hash")
        try:
            self._keys.verify_key(signature.key_id).verify(
                base64.b64decode(signature.signature_b64), bytes.fromhex(content_hash_hex)
            )
        except InvalidSignature as exc:
            raise IntegrityError("signature verification failed") from exc
