"""Versioned authenticated encryption for Telegram session material.

Canonical boundary for accounts.session_string. Application modules must not
perform AES operations or parse envelopes directly.
"""

from __future__ import annotations

import base64
import contextvars
import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.types import Text, TypeDecorator

ENVELOPE_PREFIX = "enc:v1:aes256gcm:"
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
VALID_MODES = {"disabled", "transition", "encrypted-only"}
RECORD_TYPE = "accounts.session_string"
AAD_PREFIX = "autostory:telegram-session:v1"

# Optional bind for migration / explicit APIs (stable account id).
_account_aad_ctx: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "telegram_session_aad_account_id", default=None
)

_metrics_lock = threading.Lock()
_METRICS: dict[str, int] = {
    "telegram_session_decrypt_failures": 0,
    "telegram_session_missing_key_failures": 0,
    "telegram_session_migrations_total": 0,
    "telegram_session_reencryptions_total": 0,
}


class SessionMaterialError(ValueError):
    """Base error; messages deliberately never contain session material."""


class SessionMaterialConfigurationError(SessionMaterialError):
    pass


class SessionMaterialDecryptionError(SessionMaterialError):
    pass


def _bump(metric: str) -> None:
    with _metrics_lock:
        _METRICS[metric] = int(_METRICS.get(metric, 0)) + 1


def get_session_material_metrics() -> dict[str, int]:
    with _metrics_lock:
        return dict(_METRICS)


def reset_session_material_metrics_for_tests() -> None:
    with _metrics_lock:
        for key in list(_METRICS):
            _METRICS[key] = 0


def session_aad_account_id(account_id: Optional[int]):
    """Context manager / token helper to bind AAD to account id when available."""
    return _account_aad_ctx.set(int(account_id) if account_id is not None else None)


def reset_session_aad_account_id(token) -> None:
    _account_aad_ctx.reset(token)


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
    def looks_like_envelope(value: str | None) -> bool:
        """True for any enc: prefix — including unsupported versions (fail closed)."""
        return bool(value and value.startswith("enc:"))

    def _aad(self, key_id: str) -> bytes:
        """Associated data binds envelope to app + record type + key id.

        Account ID is intentionally not required here so SQLAlchemy
        ``EncryptedSessionText`` can encrypt/decrypt without row context.
        Optional account binding remains available via contextvar for
        specialized tooling that sets and clears it symmetrically.
        """
        account_id = _account_aad_ctx.get()
        base = f"{AAD_PREFIX}:{RECORD_TYPE}:{key_id}"
        if account_id is not None:
            base = f"{base}:account:{int(account_id)}"
        return base.encode("utf-8")

    def safe_key_metadata(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "active_key_id": self.active_key_id,
            "configured_key_ids": sorted(self.keys.keys()),
            "key_material_present": bool(self.keys),
            "record_type": RECORD_TYPE,
            "envelope_prefix": ENVELOPE_PREFIX,
        }

    def inspect_format(self, stored: str | None) -> dict[str, Any]:
        if stored is None or stored == "":
            return {"format": "null_or_empty", "encrypted": False, "key_id": None}
        if self.looks_like_envelope(stored) and not self.is_encrypted(stored):
            return {
                "format": "unknown_envelope",
                "encrypted": True,
                "key_id": None,
            }
        if self.is_encrypted(stored):
            parts = stored.split(":")
            key_id = parts[3] if len(parts) >= 4 else None
            return {
                "format": "enc_v1_aes256gcm",
                "encrypted": True,
                "key_id": key_id,
                "envelope_fields": len(parts),
            }
        # Path-shaped vs opaque string — never echo value
        path_like = stored.startswith("/") or stored.endswith(".session") or stored.startswith("file://")
        return {
            "format": "legacy_path" if path_like else "legacy_plaintext",
            "encrypted": False,
            "key_id": None,
        }

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return plaintext
        if self.is_encrypted(plaintext):
            # Fail closed on nested encryption; decrypt validates, then return unchanged.
            self.decrypt(plaintext)
            return plaintext
        if self.mode == "disabled":
            return plaintext
        if not self.active_key_id or self.active_key_id not in self.keys:
            _bump("telegram_session_missing_key_failures")
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
        if self.looks_like_envelope(stored) and not self.is_encrypted(stored):
            _bump("telegram_session_decrypt_failures")
            raise SessionMaterialDecryptionError(
                "Unsupported Telegram session encryption envelope"
            )
        if not self.is_encrypted(stored):
            if self.mode == "encrypted-only":
                _bump("telegram_session_decrypt_failures")
                raise SessionMaterialDecryptionError(
                    "Legacy plaintext Telegram session material is rejected"
                )
            return stored
        parts = stored.split(":")
        if len(parts) != 6 or parts[:3] != ["enc", "v1", "aes256gcm"]:
            _bump("telegram_session_decrypt_failures")
            raise SessionMaterialDecryptionError(
                "Unsupported Telegram session encryption envelope"
            )
        key_id = parts[3]
        key = self.keys.get(key_id)
        if key is None:
            _bump("telegram_session_missing_key_failures")
            raise SessionMaterialDecryptionError(
                "Telegram session encryption key is unavailable"
            )
        try:
            nonce = base64.urlsafe_b64decode(parts[4].encode("ascii"))
            ciphertext = base64.urlsafe_b64decode(parts[5].encode("ascii"))
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, self._aad(key_id))
            return plaintext.decode("utf-8")
        except SessionMaterialError:
            raise
        except Exception as exc:
            _bump("telegram_session_decrypt_failures")
            raise SessionMaterialDecryptionError(
                "Telegram session material failed authentication"
            ) from exc

    def reencrypt(self, stored: str, *, target_key_id: Optional[str] = None) -> str:
        plaintext = self.decrypt(stored)
        target = target_key_id or self.active_key_id
        if not target or target not in self.keys:
            raise SessionMaterialConfigurationError(
                "Target Telegram session encryption key is unavailable"
            )
        writer = SessionMaterialService(
            mode=self.mode if self.mode != "disabled" else "transition",
            active_key_id=target,
            keys=self.keys,
        )
        out = writer.encrypt(plaintext)
        _bump("telegram_session_reencryptions_total")
        return out

    def to_storage(self, value: str | None) -> str | None:
        return None if value is None else self.encrypt(value)

    def from_storage(self, value: str | None) -> str | None:
        return None if value is None else self.decrypt(value)


# Public alias matching Phase 0.8 naming.
TelegramSessionMaterialService = SessionMaterialService


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
