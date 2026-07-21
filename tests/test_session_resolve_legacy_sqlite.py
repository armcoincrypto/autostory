"""Legacy / incompatible Telethon SQLite session files must not crash resolvers."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.clients.session_resolve import (
    ERR_LEGACY_SQLITE_SESSION_FORMAT,
    human_message_for_code,
    resolve_telethon_session,
)


def test_legacy_sqlite_valueerror_returns_error_code(tmp_path: Path) -> None:
    p = tmp_path / "account_9.session"
    p.write_bytes(b"x")
    acc = SimpleNamespace(id=9, session_string="", session_path=str(p))
    with patch(
        "src.clients.session_resolve.SQLiteSession",
        side_effect=ValueError("too many values to unpack (expected 5)"),
    ):
        sess, kind, err = resolve_telethon_session(acc)
    assert sess is None
    assert kind == "file"
    assert err == ERR_LEGACY_SQLITE_SESSION_FORMAT


def test_legacy_sqlite_skips_to_next_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.session"
    good = tmp_path / "good.session"
    bad.write_bytes(b"x")
    good.write_bytes(b"y")
    acc = SimpleNamespace(
        id=99,
        session_string=str(bad),
        session_path=str(good),
    )

    calls: list[str] = []

    def _fake_sqlite(base: str) -> MagicMock:
        calls.append(base)
        if base.endswith("bad"):
            raise ValueError("too many values to unpack (expected 5)")
        m = MagicMock()
        return m

    miss = tmp_path / "nope.session"
    with patch(
        "src.core.session_paths.get_canonical_session_path",
        return_value=miss,
    ), patch("src.clients.session_resolve.SQLiteSession", side_effect=_fake_sqlite):
        sess, kind, err = resolve_telethon_session(acc)
    assert err is None
    assert kind == "file"
    assert sess is not None
    assert any("bad" in c for c in calls)
    assert any("good" in c for c in calls)


def test_human_message_legacy_sqlite() -> None:
    assert (
        human_message_for_code(ERR_LEGACY_SQLITE_SESSION_FORMAT)
        == "Legacy or incompatible Telegram session format"
    )


def test_prefer_string_over_file_before_sqlite(tmp_path: Path) -> None:
    """Kathleen path: valid string session must win over an on-disk SQLite file."""
    p = tmp_path / "account_2.session"
    p.write_bytes(b"x")
    acc = SimpleNamespace(
        id=2,
        session_string="not-a-real-but-nonempty-string",
        session_path=str(p),
    )
    mock_string_sess = MagicMock()
    with patch(
        "src.clients.session_resolve.SQLiteSession",
        side_effect=AssertionError("SQLiteSession must not run when string wins"),
    ), patch(
        "src.clients.session_resolve.StringSession",
        return_value=mock_string_sess,
    ) as ss_ctor:
        sess, kind, err = resolve_telethon_session(acc, prefer_string_over_file=True)
    ss_ctor.assert_called_once()
    assert err is None
    assert kind == "string"
    assert sess is mock_string_sess
