"""Fail if high-risk modules construct Telethon sessions outside resolve_telethon_session."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Application modules that must not ad-hoc build StringSession/SQLiteSession for accounts.
GUARDED = [
    ROOT / "src/discovery/scanner.py",
    ROOT / "src/dashboard/routes.py",
]


def test_guarded_modules_use_resolve_not_ad_hoc_string_session() -> None:
    for path in GUARDED:
        text = path.read_text(encoding="utf-8")
        assert "resolve_telethon_session" in text, f"{path} must call resolve_telethon_session"
        # Disallow classic ad-hoc constructions in these files
        assert "StringSession(session_val" not in text
        assert "StringSession(session_string)" not in text
        assert "SQLiteSession(sess_path)" not in text


def test_healthcheck_helper_does_not_construct_string_session_directly() -> None:
    text = (ROOT / "src/clients/manager.py").read_text(encoding="utf-8")
    # The helper must delegate to resolve_telethon_session
    start = text.index("def _existing_session_source_for_healthcheck")
    end = text.index("\nclass ", start)
    helper = text[start:end]
    assert "resolve_telethon_session" in helper
    assert "StringSession(" not in helper


def test_session_to_string_export_requires_ack() -> None:
    text = (ROOT / "scripts/session_to_string.py").read_text(encoding="utf-8")
    assert "ACKNOWLEDGE_SESSION_STRING_EXPORT" in text


def test_legacy_fernet_managers_are_gated() -> None:
    core = (ROOT / "src/core/session_manager.py").read_text(encoding="utf-8")
    clients = (ROOT / "src/clients/session.py").read_text(encoding="utf-8")
    assert "LEGACY_FERNET_SESSION_MANAGER_ENABLED" in core
    assert "LEGACY_FERNET_SESSION_MANAGER_ENABLED" in clients


def test_account_session_column_uses_encrypted_type() -> None:
    models = (ROOT / "src/core/models.py").read_text(encoding="utf-8")
    assert "EncryptedSessionText" in models
    assert "session_string = Column(EncryptedSessionText()" in models
