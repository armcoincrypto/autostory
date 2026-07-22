"""Readiness disposable session path — no exclusive lock on source session."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.clients.session_sqlite_copy import copy_sqlite_session_readonly


def test_copy_sqlite_session_readonly_roundtrip(tmp_path: Path):
    import sqlite3

    src = tmp_path / "account_140.session"
    dst = tmp_path / "copy" / "account_140.session"
    with sqlite3.connect(src) as conn:
        conn.execute("CREATE TABLE version (number INTEGER)")
        conn.execute("INSERT INTO version VALUES (7)")
        conn.commit()
    copy_sqlite_session_readonly(src, dst)
    assert dst.is_file()
    with sqlite3.connect(dst) as conn:
        assert conn.execute("SELECT number FROM version").fetchone()[0] == 7


@pytest.mark.asyncio
async def test_connect_account_disposable_skips_exclusive_lock(tmp_path: Path):
    from src.clients import manager as manager_mod
    from src.core.models import Account

    src = tmp_path / "account_9.session"
    src.write_bytes(b"placeholder")

    account = Account(id=9, phone_number="+10000000009", session_string=str(src))
    session_obj = MagicMock()
    session_obj.filename = str(src.with_suffix(""))

    lock_calls: list[bool] = []

    def _fake_lock(*_a, **_k):
        lock_calls.append(True)
        return False, None, "should_not_lock"

    class _FakeClient:
        def __init__(self, *_a, **_k):
            self._connected = False

        async def connect(self):
            self._connected = True

        async def is_user_authorized(self):
            return True

        def is_connected(self):
            return self._connected

        async def disconnect(self):
            self._connected = False

    cm = manager_mod.ClientManager.__new__(manager_mod.ClientManager)
    cm._clients = {}
    cm._lock = asyncio.Lock()
    cm._rate_limiter = MagicMock()

    class _Ctx:
        def __enter__(self):
            db = MagicMock()
            db.query.return_value.filter.return_value.first.return_value = account
            return db

        def __exit__(self, *args):
            return False

    with patch("src.clients.manager.get_db_context", return_value=_Ctx()), patch(
        "src.clients.manager.resolve_telethon_session",
        return_value=(session_obj, "file", None),
    ), patch(
        "src.clients.manager.acquire_session_lock",
        side_effect=_fake_lock,
    ), patch(
        "src.clients.manager.TelegramClient",
        _FakeClient,
    ), patch(
        "src.clients.manager.SQLiteSession",
        return_value=MagicMock(name="disposable_session"),
    ), patch(
        "src.clients.session_sqlite_copy.copy_sqlite_session_readonly",
    ) as copy_fn, patch(
        "src.clients.manager.settings"
    ) as settings:
        settings.telegram.api_id = 1
        settings.telegram.api_hash = "hash"
        # connect_account imports copy helper inside the method
        with patch(
            "src.clients.session_sqlite_copy.copy_sqlite_session_readonly",
            copy_fn,
        ):
            wrapper, err = await cm.connect_account(9, disposable_file_session=True)

    assert err is None
    assert wrapper is not None
    assert wrapper._disposable_session_dir is not None
    assert lock_calls == []
    assert copy_fn.called
    temp_dir = wrapper._disposable_session_dir
    await cm.remove_account(9)
    assert not Path(temp_dir).exists()
