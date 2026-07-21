"""Versioned authenticated encryption for Telegram session material."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.types import Text, TypeDecorator


ENVELOPE_PREFIX = "enc:v1:aes256gcm:"
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
VALID_MODES = {"disabled", "transition", "encrypted-only"}


class SessionMaterialError(ValueError):
    """Base error; messages deliberately never contain session material."""


class SessionMaterialConfigurationError(SessionMaterialError):
    pass


class SessionMaterialDecryptionError(SessionMaterialError):
    pass


def _decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:
        raise SessionMaterialConfigurationError(
            "Telegram session encryption key is not valid base64"
        ) from exc
    if len(key) != 32:
        raise SessionMaterialConfigurationError(
            "Telegram session encryption key must decode to 32 bytes"
        )
    return key


@dataclass(frozen=True)
class SessionMaterialService:
    mode: str
    active_key_id: str | None
    keys: dict[str, bytes]

    @classmethod
    def from_environment(cls) -> "SessionMaterialService":
        environment = os.environ.get("ENVIRONMENT", "development").strip().lower()
        default_mode = "transition" if environment in {"production", "prod"} else "disabled"
        mode = os.environ.get(
            "TELEGRAM_SESSION_ENCRYPTION_MODE", default_mode
        ).strip().lower()
        if mode not in VALID_MODES:
            raise SessionMaterialConfigurationError(
                "Unknown TELEGRAM_SESSION_ENCRYPTION_MODE"
            )

        keys: dict[str, bytes] = {}
        keys_json = os.environ.get("TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON", "").strip()
        if keys_json:
            try:
                decoded = json.loads(keys_json)
            except json.JSONDecodeError as exc:
                raise SessionMaterialConfigurationError(
                    "TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON is invalid"
                ) from exc
            if not isinstance(decoded, dict):
                raise SessionMaterialConfigurationError(
                    "TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON must be an object"
                )
            for key_id, encoded in decoded.items():
                if not isinstance(key_id, str) or not KEY_ID_PATTERN.match(key_id):
                    raise SessionMaterialConfigurationError("Invalid encryption key identifier")
                if not isinstance(encoded, str):
                    raise SessionMaterialConfigurationError("Invalid encryption key value")
                keys[key_id] = _decode_key(encoded)

        active_key_id = os.environ.get(
            "TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID", ""
        ).strip()
        single_key = os.environ.get("TELEGRAM_SESSION_ENCRYPTION_KEY_B64", "").strip()
        if single_key:
            if not active_key_id or not KEY_ID_PATTERN.match(active_key_id):
                raise SessionMaterialConfigurationError(
                    "A valid active key identifier is required"
                )
            keys[active_key_id] = _decode_key(single_key)

        if mode in {"transition", "encrypted-only"}:
            if not active_key_id or active_key_id not in keys:
                raise SessionMaterialConfigurationError(
                    "Active Telegram session encryption key is not configured"
                )
        return cls(mode=mode, active_key_id=active_key_id or None, keys=keys)

    @staticmethod
    def is_encrypted(value: str | None) -> bool:
        return bool(value and value.startswith(ENVELOPE_PREFIX))

    @staticmethod
    def _aad(key_id: str) -> bytes:
        return f"autostory:telegram-session:v1:{key_id}".encode("utf-8")

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return plaintext
        if self.is_encrypted(plaintext):
            self.decrypt(plaintext)
            return plaintext
        if self.mode == "disabled":
            return plaintext
        if not self.active_key_id or self.active_key_id not in self.keys:
            raise SessionMaterialConfigurationError(
                "Active Telegram session encryption key is unavailable"
            )
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.keys[self.active_key_id]).encrypt(
            nonce, plaintext.encode("utf-8"), self._aad(self.active_key_id)
        )
        return ":".join(
            (
                "enc",
                "v1",
                "aes256gcm",
                self.active_key_id,
                base64.urlsafe_b64encode(nonce).decode("ascii"),
                base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            )
        )

    def decrypt(self, stored: str) -> str:
        if not stored:
            return stored
        if not self.is_encrypted(stored):
            if self.mode == "encrypted-only":
                raise SessionMaterialDecryptionError(
                    "Legacy plaintext Telegram session material is rejected"
                )
            return stored
        parts = stored.split(":")
        if len(parts) != 6 or parts[:3] != ["enc", "v1", "aes256gcm"]:
            raise SessionMaterialDecryptionError(
                "Unsupported Telegram session encryption envelope"
            )
        key_id = parts[3]
        key = self.keys.get(key_id)
        if key is None:
            raise SessionMaterialDecryptionError(
                "Telegram session encryption key is unavailable"
            )
        try:
            nonce = base64.urlsafe_b64decode(parts[4].encode("ascii"))
            ciphertext = base64.urlsafe_b64decode(parts[5].encode("ascii"))
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, self._aad(key_id))
            return plaintext.decode("utf-8")
        except Exception as exc:
            raise SessionMaterialDecryptionError(
                "Telegram session material failed authentication"
            ) from exc

    def to_storage(self, value: str | None) -> str | None:
        return None if value is None else self.encrypt(value)

    def from_storage(self, value: str | None) -> str | None:
        return None if value is None else self.decrypt(value)


class EncryptedSessionText(TypeDecorator):
    """SQLAlchemy boundary that centralizes every ORM session read and write."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        del dialect
        return SessionMaterialService.from_environment().to_storage(value)

    def process_result_value(self, value, dialect):
        del dialect
        return SessionMaterialService.from_environment().from_storage(value)
