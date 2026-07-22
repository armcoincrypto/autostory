"""Usable Story session = canonical file OR resolvable session_string."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.session_paths import account_has_canonical_session, account_has_usable_story_session


def test_usable_session_true_for_resolvable_string_without_file():
    account = SimpleNamespace(id=140, session_path=None, session_string="encrypted-or-plain")
    with patch(
        "src.core.session_paths.get_canonical_session_path",
        return_value=Path("/tmp/missing-account_140.session"),
    ), patch(
        "src.clients.session_resolve.probe_telethon_session_kind",
        return_value=("string", None),
    ):
        assert account_has_canonical_session(account) is False
        assert account_has_usable_story_session(account) is True


def test_usable_session_false_when_unresolvable():
    account = SimpleNamespace(id=141, session_path=None, session_string="")
    with patch(
        "src.core.session_paths.get_canonical_session_path",
        return_value=Path("/tmp/missing-account_141.session"),
    ), patch(
        "src.clients.session_resolve.probe_telethon_session_kind",
        return_value=("empty", "empty_session"),
    ):
        assert account_has_usable_story_session(account) is False


def test_usable_session_true_when_canonical_file_present(tmp_path: Path):
    session_file = tmp_path / "account_140.session"
    session_file.write_bytes(b"x")
    account = SimpleNamespace(id=140, session_path=None, session_string=None)
    with patch(
        "src.core.session_paths.get_canonical_session_path",
        return_value=session_file,
    ):
        assert account_has_canonical_session(account) is True
        assert account_has_usable_story_session(account) is True
