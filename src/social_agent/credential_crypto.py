"""Dedicated AES-256-GCM encryption for Social Agent provider credentials.

Separate from Telegram session encryption keys and key IDs.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_PREFIX = "enc:v1:aes256gcm:"
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
AAD = b"zellotex:social-agent-credential:v1"


class CredentialCryptoError(ValueError):
    """Safe error — never include plaintext or key material in messages."""


class CredentialCryptoConfigurationError(CredentialCryptoError):
    pass


def _decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:
        raise CredentialCryptoConfigurationError("Social credential key is not valid base64") from exc
    if len(key) != 32:
        raise CredentialCryptoConfigurationError("Social credential key must decode to 32 bytes")
    return key


@dataclass(frozen=True)
class SocialCredentialCrypto:
    active_key_id: str
    keys: dict[str, bytes]

    @classmethod
    def from_environment(cls) -> "SocialCredentialCrypto":
        active = (os.environ.get("SOCIAL_CREDENTIAL_ACTIVE_KEY_ID") or "").strip()
        raw = (os.environ.get("SOCIAL_CREDENTIAL_KEYS_JSON") or os.environ.get("SOCIAL_CREDENTIAL_KEYS") or "").strip()
        if not active or not raw:
            raise CredentialCryptoConfigurationError("Social credential encryption keys are not configured")
        if not KEY_ID_PATTERN.match(active):
            raise CredentialCryptoConfigurationError("Invalid SOCIAL_CREDENTIAL_ACTIVE_KEY_ID")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialCryptoConfigurationError("SOCIAL_CREDENTIAL_KEYS_JSON is invalid") from exc
        if not isinstance(decoded, dict) or not decoded:
            raise CredentialCryptoConfigurationError("SOCIAL_CREDENTIAL_KEYS_JSON must be a non-empty object")
        keys: dict[str, bytes] = {}
        for kid, val in decoded.items():
            kid_s = str(kid).strip()
            if not KEY_ID_PATTERN.match(kid_s):
                raise CredentialCryptoConfigurationError("Invalid encryption key identifier")
            keys[kid_s] = _decode_key(str(val).strip())
        if active not in keys:
            raise CredentialCryptoConfigurationError("Active social credential key id is missing from key ring")
        return cls(active_key_id=active, keys=keys)

    @classmethod
    def configured(cls) -> bool:
        try:
            cls.from_environment()
            return True
        except CredentialCryptoConfigurationError:
            return False

    def encrypt(self, plaintext: str) -> str:
        key = self.keys[self.active_key_id]
        nonce = secrets.token_bytes(12)
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), AAD)
        return (
            f"{ENVELOPE_PREFIX}{self.active_key_id}:"
            f"{base64.urlsafe_b64encode(nonce).decode('ascii')}:"
            f"{base64.urlsafe_b64encode(ct).decode('ascii')}"
        )

    def decrypt(self, envelope: str) -> str:
        if not envelope or not envelope.startswith(ENVELOPE_PREFIX):
            raise CredentialCryptoError("Unrecognized credential envelope")
        parts = envelope[len(ENVELOPE_PREFIX) :].split(":")
        if len(parts) != 3:
            raise CredentialCryptoError("Malformed credential envelope")
        kid, nonce_b64, ct_b64 = parts
        key = self.keys.get(kid)
        if key is None:
            raise CredentialCryptoError("Credential encryption key is unavailable")
        try:
            nonce = base64.urlsafe_b64decode(nonce_b64.encode("ascii"))
            ct = base64.urlsafe_b64decode(ct_b64.encode("ascii"))
            pt = AESGCM(key).decrypt(nonce, ct, AAD)
        except Exception as exc:
            raise CredentialCryptoError("Credential decryption failed") from exc
        return pt.decode("utf-8")

    def encrypt_json(self, payload: dict[str, Any]) -> str:
        return self.encrypt(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def decrypt_json(self, envelope: str) -> dict[str, Any]:
        data = json.loads(self.decrypt(envelope))
        if not isinstance(data, dict):
            raise CredentialCryptoError("Credential payload must be an object")
        return data
