"""Focused safety tests for existing-account reauth helper (no live Telegram)."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/ops/reauth_existing_publishing_account.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("reauth_existing_publishing_account", HELPER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_helper_has_no_publish_or_message_surface() -> None:
    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    banned = {
        "SendStoryRequest",
        "send_message",
        "SendMessageRequest",
        "create_story_run",
        "StoryRun",
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert not (names & banned)
    text = HELPER.read_text(encoding="utf-8")
    assert "CanSendStoryRequest" in text
    assert "getpass.getpass" in text
    assert "qr-url.txt" not in text
    assert "from telethon.tl.functions.stories import CanSendStoryRequest" in text
    assert "SendStoryRequest," not in text
    assert "SendStoryRequest)" not in text


def test_assert_publishing_intent_rejects_protected(monkeypatch) -> None:
    mod = _load_helper()
    monkeypatch.setattr(mod, "CONTROLLER_ACCOUNT_IDS", {13})
    monkeypatch.setattr(mod, "PROTECTED_IDS", set())
    monkeypatch.setattr(mod, "RESERVED_AI_AGENT_ACCOUNT_IDS", set())
    monkeypatch.setattr(mod, "PURPOSE_HOLD_IDS", set())
    account = SimpleNamespace(
        id=13,
        status="active",
        purpose="both",
        user_id=7707041428,
        phone_number="+10000000000",
    )
    with pytest.raises(SystemExit, match="CONFIG_REVIEW_REQUIRED"):
        mod.assert_publishing_intent(account)


def test_phone_reauth_requires_tty(monkeypatch) -> None:
    mod = _load_helper()
    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: False)

    async def _run():
        return await mod.reauth_phone(
            SimpleNamespace(id=13, phone_number="+10000000000", user_id=1, username=None),
            Path("/tmp"),
        )

    import asyncio

    result = asyncio.run(_run())
    assert result["success"] is False
    assert result["error"] == "NO_TTY"


def test_identity_mismatch_does_not_persist(monkeypatch) -> None:
    mod = _load_helper()
    persisted: list[tuple[int, str]] = []

    class _FakeMe:
        id = 999
        username = "wrong"

    class _FakeQR:
        url = "tg://login?token=SECRET_TOKEN"

        async def wait(self, timeout=None):
            return _FakeMe()

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.session = SimpleNamespace(save=lambda: "NEW_SESSION_SHOULD_NOT_PERSIST")

        async def connect(self):
            return None

        async def qr_login(self):
            return _FakeQR()

        async def get_me(self):
            return _FakeMe()

        async def disconnect(self):
            return None

        async def __call__(self, *args, **kwargs):
            raise AssertionError("story probe must not run after identity mismatch")

    async def _persist(account_id, session_string):
        persisted.append((account_id, session_string))

    monkeypatch.setattr(mod, "TelegramClient", _FakeClient)
    monkeypatch.setattr(mod, "persist_session_string", _persist)
    monkeypatch.setattr(mod, "backup_encrypted_session_blob", lambda _aid: {"ok": True})

    account = SimpleNamespace(id=13, user_id=7707041428, username="Donolondol_9")

    import asyncio

    result = asyncio.run(mod.reauth_qr(account, Path("/tmp/reauth-test-evidence"), 5.0))
    assert result["success"] is False
    assert result["error"] == "IDENTITY_MISMATCH"
    assert persisted == []
    assert not list(Path("/tmp/reauth-test-evidence").glob("*-qr-url.txt"))


def test_auth_result_redacts_secrets() -> None:
    mod = _load_helper()
    sample = {
        "success": True,
        "session_string": "SECRET",
        "password": "SECRET",
        "code": "12345",
        "otp": "12345",
        "qr_url": "tg://login?token=SECRET",
        "url": "tg://login?token=SECRET",
        "classification": "READY_FOR_SEPARATE_CONTROLLED_CANARY",
    }
    for key in ("session_string", "password", "code", "otp", "qr_url", "url"):
        sample.pop(key, None)
    dumped = str(sample)
    assert "SECRET" not in dumped
    assert "12345" not in dumped


def test_backup_encrypted_session_blob_writes_restricted_file(monkeypatch, tmp_path) -> None:
    mod = _load_helper()
    monkeypatch.setattr(mod, "ROLLBACK_ROOT", tmp_path / "rollbacks")

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(fetchone=lambda: ("enc-blob-value",))

    class _Engine:
        def connect(self):
            return _Conn()

    monkeypatch.setattr(mod, "engine", _Engine())
    meta = mod.backup_encrypted_session_blob(13)
    assert meta["ok"] is True
    path = Path(meta["path"])
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8") == "enc-blob-value"
    assert meta["sha256"]
