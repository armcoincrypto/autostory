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


NEW_MIGRATION = ROOT / "scripts" / "db" / "migrate_telegram_session_encryption.py"


def run_new_migration(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(NEW_MIGRATION), *args, str(path)],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        cwd=ROOT,
    )


def test_nonce_uniqueness_and_aad_binding(encryption_env) -> None:
    service = SessionMaterialService.from_environment()
    a = service.encrypt("same-plaintext")
    b = service.encrypt("same-plaintext")
    assert a != b
    # Wrong AAD (account-bound) must fail for unbound ciphertext
    from src.security.session_material import session_aad_account_id, reset_session_aad_account_id

    token = session_aad_account_id(42)
    try:
        with pytest.raises(SessionMaterialDecryptionError):
            SessionMaterialService.from_environment().decrypt(a)
    finally:
        reset_session_aad_account_id(token)


def test_disabled_mode_leaves_plaintext(monkeypatch) -> None:
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "disabled")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID", "test-key")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_KEY_B64", key)
    service = SessionMaterialService.from_environment()
    assert service.to_storage("plain") == "plain"
    # Encrypted values remain decryptable when key is present
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "transition")
    enc = SessionMaterialService.from_environment().encrypt("secret")
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "disabled")
    assert SessionMaterialService.from_environment().from_storage(enc) == "secret"


def test_double_encryption_is_idempotent_not_nested(encryption_env) -> None:
    service = SessionMaterialService.from_environment()
    once = service.encrypt("payload")
    twice = service.encrypt(once)
    assert twice == once


def test_malformed_envelope_rejected(encryption_env) -> None:
    service = SessionMaterialService.from_environment()
    with pytest.raises(SessionMaterialDecryptionError):
        service.decrypt("enc:v1:aes256gcm:only-three")
    with pytest.raises(SessionMaterialDecryptionError):
        service.decrypt("enc:v9:aes256gcm:test-key:aa:bb")


def test_invalid_mode_rejected(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "encrypt-everything")
    with pytest.raises(SessionMaterialConfigurationError):
        SessionMaterialService.from_environment()


def test_new_migration_tool_refuses_production_path(encryption_env, tmp_path: Path) -> None:
    # Create a fake protected path equal to a disposable db
    database = tmp_path / "storyfleet.db"
    create_mixed_database(database, SessionMaterialService.from_environment().encrypt("x"))
    result = run_new_migration(
        database,
        "migrate",
        "--expected-plaintext",
        "1",
        "--protected-path",
        str(database),
        "--acknowledge-disposable-copy",
    )
    assert result.returncode != 0
    assert "refusing protected" in result.stderr


def test_new_migration_tool_idempotent(encryption_env, tmp_path: Path) -> None:
    encrypted = SessionMaterialService.from_environment().encrypt("already")
    database = tmp_path / "mig.db"
    create_mixed_database(database, encrypted)
    first = run_new_migration(
        database,
        "migrate",
        "--expected-plaintext",
        "1",
        "--acknowledge-disposable-copy",
        "--json",
    )
    second = run_new_migration(
        database,
        "migrate",
        "--expected-plaintext",
        "0",
        "--acknowledge-disposable-copy",
        "--json",
    )
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["migrated_rows"] == 1
    assert json.loads(second.stdout)["migrated_rows"] == 0


def test_encrypted_only_rejects_filesystem_path_and_fernet(encryption_env, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "encrypted-only")
    service = SessionMaterialService.from_environment()
    with pytest.raises(SessionMaterialDecryptionError):
        service.from_storage("/opt/autostory/data/sessions/account_1.session")
    with pytest.raises(SessionMaterialDecryptionError):
        service.from_storage("gAAAAABnot-a-real-fernet-token")


def test_encrypted_only_write_stores_envelope(encryption_env, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_SESSION_ENCRYPTION_MODE", "encrypted-only")
    service = SessionMaterialService.from_environment()
    stored = service.to_storage("unit-test-session-material")
    assert stored is not None and stored.startswith(ENVELOPE_PREFIX)
    assert service.from_storage(stored) == "unit-test-session-material"
