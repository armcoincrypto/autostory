"""Shallow account readiness (deep=0): DB-light path must not open Telethon SQLiteSession."""
from __future__ import annotations

from pathlib import Path

import pytest

import src.core.models  # noqa: F401
import src.core.scheduler_models  # noqa: F401
from src.clients.session_resolve import probe_telethon_session_kind


def test_probe_telethon_session_kind_file_exists_without_sqlite_session(tmp_path):
    """Corrupt / non-Telethon bytes must not crash shallow probe (Telethon SQLiteSession would)."""
    p = tmp_path / "acc.session"
    p.write_bytes(b"not valid telethon session sqlite format")

    class Acc:
        id = 1
        session_string = ""
        session_path = str(p)

    kind, err = probe_telethon_session_kind(Acc())
    assert kind == "file"
    assert err is None


def test_probe_telethon_session_kind_empty_account():
    class Acc:
        id = 99
        session_string = ""
        session_path = ""

    kind, err = probe_telethon_session_kind(Acc())
    assert kind == "empty"
    assert err == "empty_session"


@pytest.mark.asyncio
async def test_readiness_row_shallow_never_calls_resolve_telethon_session(monkeypatch):
    """``_readiness_row(..., deep=False)`` must use probe only (no SQLiteSession / no resolve)."""
    from src.clients import manager as manager_mod

    def _boom(_account):
        raise AssertionError("resolve_telethon_session must not run for deep=False")

    monkeypatch.setattr(manager_mod, "resolve_telethon_session", _boom)
    monkeypatch.setattr(manager_mod, "probe_telethon_session_kind", lambda _a: ("file", None))

    class StubAccount:
        id = 42
        phone_number = "+19995550101"
        first_name = None
        last_name = None
        session_string = ""

    row = await manager_mod.client_manager._readiness_row(StubAccount(), deep=False)  # type: ignore[arg-type]
    assert row["account_id"] == 42
    assert row["session_kind"] == "file"
    assert row["session_exists"] is True
    assert row["ready"] is False
    assert row["authorized"] is None
