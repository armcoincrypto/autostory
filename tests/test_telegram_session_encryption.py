from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select

from src.security.session_material import (
    ENVELOPE_PREFIX,
    EncryptedSessionText,
    SessionMaterialConfigurationError,
    SessionMaterialDecryptionError,
    SessionMaterialService,
)


ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "scripts" / "db" / "rehearse_telegram_session_encryption.py"


@pytest.fixture
def encryption_env(monkeypatch):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "transition")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID", "test-key")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_KEY_B64", key)
    monkeypatch.delenv("TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON", raising=False)
    return key


def test_transition_reads_legacy_and_encrypts_new_values(encryption_env) -> None:
    service = SessionMaterialService.from_environment()

    encrypted = service.to_storage("legacy-session")

    assert service.from_storage("legacy-session") == "legacy-session"
    assert encrypted.startswith(f"{ENVELOPE_PREFIX}test-key:")
    assert service.from_storage(encrypted) == "legacy-session"


def test_tamper_wrong_key_and_missing_key_fail_without_material_in_error(
    encryption_env, monkeypatch
) -> None:
    secret = "must-never-appear-in-errors"
    encrypted = SessionMaterialService.from_environment().encrypt(secret)
    tampered = encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B")

    with pytest.raises(SessionMaterialDecryptionError) as tamper_error:
        SessionMaterialService.from_environment().decrypt(tampered)
    assert secret not in str(tamper_error.value)

    wrong = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_KEY_B64", wrong)
    with pytest.raises(SessionMaterialDecryptionError):
        SessionMaterialService.from_environment().decrypt(encrypted)

    monkeypatch.delenv("TELEGRAM_SESSION_ENCRYPTION_KEY_B64")
    with pytest.raises(SessionMaterialConfigurationError):
        SessionMaterialService.from_environment()


def test_encrypted_only_rejects_plaintext(encryption_env, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "encrypted-only")

    with pytest.raises(SessionMaterialDecryptionError):
        SessionMaterialService.from_environment().from_storage("legacy")


def test_production_without_explicit_key_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("TELEGRAM_SESSION_ENCRYPTION_MODE", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_ENCRYPTION_KEY_B64", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON", raising=False)

    with pytest.raises(SessionMaterialConfigurationError):
        SessionMaterialService.from_environment()


def test_sqlalchemy_type_centralizes_storage_boundary(encryption_env) -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("material", EncryptedSessionText()),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(sessions.insert().values(id=1, material="plain"))
        raw = connection.exec_driver_sql(
            "SELECT material FROM sessions WHERE id=1"
        ).scalar_one()
        loaded = connection.execute(
            select(sessions.c.material).where(sessions.c.id == 1)
        ).scalar_one()

    assert raw.startswith(ENVELOPE_PREFIX)
    assert loaded == "plain"


def create_mixed_database(path: Path, encrypted: str) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                session_string TEXT
            );
            """
        )
        db.executemany(
            "INSERT INTO accounts VALUES (?, ?)",
            [(1, "legacy-one"), (2, encrypted), (3, None)],
        )


def run_migration(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MIGRATION), str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        cwd=ROOT,
    )


def test_mixed_database_migration_is_idempotent(encryption_env, tmp_path: Path) -> None:
    encrypted = SessionMaterialService.from_environment().encrypt("already-encrypted")
    database = tmp_path / "sessions.db"
    create_mixed_database(database, encrypted)

    first = run_migration(
        database,
        "--expected-plaintext",
        "1",
        "--apply",
        "--acknowledge-disposable-copy",
        "--json",
    )
    second = run_migration(
        database,
        "--expected-plaintext",
        "0",
        "--apply",
        "--acknowledge-disposable-copy",
        "--json",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["migrated_rows"] == 1
    assert json.loads(first.stdout)["plaintext_rows_after"] == 0
    assert json.loads(second.stdout)["migrated_rows"] == 0


def test_migration_refuses_protected_database(encryption_env, tmp_path: Path) -> None:
    encrypted = SessionMaterialService.from_environment().encrypt("encrypted")
    database = tmp_path / "sessions.db"
    create_mixed_database(database, encrypted)

    result = run_migration(
        database,
        "--expected-plaintext",
        "1",
        "--protected-path",
        str(database),
        "--apply",
        "--acknowledge-disposable-copy",
    )

    assert result.returncode != 0
    assert "refusing protected/live database target" in result.stderr
