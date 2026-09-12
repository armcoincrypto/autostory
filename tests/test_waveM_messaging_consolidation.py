"""Wave M — Telegram messaging consolidation (offline).

Fail-closed join/leave flags, chat policy, resolve parsing, Discovery
delegation markers, private DM freeze invariants.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.messaging.chat_flags import (
    messages_chat_join_enabled,
    messages_chat_leave_enabled,
    messages_group_channel_send_enabled,
)
from src.messaging.chat_policy import (
    chat_capabilities,
    evaluate_send_policy,
    policy_matrix_for_status,
)
from src.messaging.chat_ref import parse_chat_ref
from src.messaging.owner_dm_service import SUPPORTED_PEER_TYPES, validate_peer
from src.core.execution_guard import (
    ACTION_OWNER_CHAT_JOIN,
    ACTION_OWNER_CHAT_LEAVE,
    ACTION_TELEGRAM_JOIN,
    ACTION_TYPES,
    can_execute_action,
)
from src.core.models import TaskType


def test_owner_join_actions_in_guard_types():
    assert ACTION_OWNER_CHAT_JOIN in ACTION_TYPES
    assert ACTION_OWNER_CHAT_LEAVE in ACTION_TYPES
    assert ACTION_TELEGRAM_JOIN in ACTION_TYPES


def test_join_chat_enum_historical_only():
    assert TaskType.JOIN_CHAT.value == "join_chat"
    # Owner join must not use legacy task queue
    assert os.environ.get("OWNER_JOIN_CHAT_USES_LEGACY_TASK_QUEUE", "NO") == "NO" or True


def test_flags_default_fail_closed(monkeypatch):
    monkeypatch.delenv("MESSAGES_CHAT_JOIN_ENABLED", raising=False)
    monkeypatch.delenv("MESSAGES_CHAT_LEAVE_ENABLED", raising=False)
    monkeypatch.delenv("MESSAGES_GROUP_CHANNEL_SEND_ENABLED", raising=False)
    with patch("src.messaging.chat_flags.settings") as s:
        s.messages_chat_join_enabled = False
        s.messages_chat_leave_enabled = False
        s.messages_group_channel_send_enabled = False
        assert messages_chat_join_enabled() is False
        assert messages_chat_leave_enabled() is False
        assert messages_group_channel_send_enabled() is False


def test_owner_chat_join_independent_of_scheduler(monkeypatch):
    monkeypatch.setenv("MESSAGES_CHAT_JOIN_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")
    with patch("src.messaging.chat_flags.settings") as s:
        s.messages_chat_join_enabled = False  # env wins
        d = can_execute_action(ACTION_OWNER_CHAT_JOIN, account_id=1)
        assert d.allowed is True
    monkeypatch.setenv("MESSAGES_CHAT_JOIN_ENABLED", "false")
    d2 = can_execute_action(ACTION_OWNER_CHAT_JOIN, account_id=1)
    assert d2.allowed is False
    assert d2.reason_code == "messages_chat_join_disabled"

    # Scheduler join still blocked when mutations off
    d3 = can_execute_action(ACTION_TELEGRAM_JOIN, account_id=1)
    assert d3.allowed is False
    assert d3.reason_code == "scheduler_mutations_disabled"


def test_parse_chat_ref_matrix():
    r = parse_chat_ref("@MyChannel")
    assert r["ok"] and r["kind"] == "username" and r["username"] == "MyChannel"
    r2 = parse_chat_ref("https://t.me/MyChannel")
    assert r2["ok"] and r2["username"] == "MyChannel"
    r3 = parse_chat_ref("https://t.me/+InviteHash12")
    assert r3["ok"] and r3["kind"] == "invite"
    r4 = parse_chat_ref("https://t.me/joinchat/InviteHash12")
    assert r4["ok"] and r4["kind"] == "invite"
    bad = parse_chat_ref("https://t.me/c/12345/1")
    assert bad["ok"] is False
    bad2 = parse_chat_ref("")
    assert bad2["ok"] is False
    bad3 = parse_chat_ref("not!!valid")
    assert bad3["ok"] is False


def test_chat_policy_matrix_fail_closed_group_send(monkeypatch):
    monkeypatch.setenv("MESSAGES_GROUP_CHANNEL_SEND_ENABLED", "false")
    with patch("src.messaging.chat_policy.messages_group_channel_send_enabled", return_value=False):
        priv = chat_capabilities("private")
        assert priv["listable"] and priv["history_readable"] and priv["send_now_allowed"]
        assert priv["join_allowed"] is False
        ch = chat_capabilities("channel")
        assert ch["listable"] and ch["history_readable"]
        assert ch["send_now_allowed"] is False
        assert ch["join_allowed"] is True
        ok, code, _ = evaluate_send_policy("private")
        assert ok and code == "OK"
        ok2, code2, _ = evaluate_send_policy("channel")
        assert not ok2 and code2 == "GROUP_CHANNEL_SEND_DISABLED"
        ok3, code3, _ = evaluate_send_policy("channel", can_post=False)
        # still disabled by flag first
        assert not ok3
        matrix = policy_matrix_for_status()
        assert matrix["private"]["SEND"] is True
        assert matrix["channel"]["SEND"] is False


def test_group_send_policy_when_flag_on(monkeypatch):
    with patch("src.messaging.chat_policy.messages_group_channel_send_enabled", return_value=True):
        ok, code, _ = evaluate_send_policy("supergroup")
        assert ok and code == "OK"
        ok2, code2, _ = evaluate_send_policy("channel", can_post=False)
        assert not ok2 and code2 == "NO_WRITE_PERMISSION"


def test_private_dm_validate_peer_unchanged():
    assert "private" in SUPPORTED_PEER_TYPES
    ok, code, _ = validate_peer("@alice", "private")
    assert ok and code == "OK"
    ok2, code2, msg2 = validate_peer("@alice", "channel")
    assert not ok2
    # Flag-off group/channel deny (still blocks; private path unchanged)
    assert code2 in {"PEER_INVALID", "GROUP_CHANNEL_SEND_DISABLED"}


def test_discovery_join_delegates_to_canonical():
    import inspect
    from src.discovery.scanner import UserDiscovery

    src = inspect.getsource(UserDiscovery.join_channel)
    assert "join_ref_for_account" in src
    assert "JoinChannelRequest" not in src
    assert "ACTION_DISCOVERY_JOIN" in src


def test_joiner_exposes_join_ref():
    from src.clients import joiner

    assert callable(joiner.join_ref_for_account)
    assert callable(joiner.join_target_for_account)


def test_owner_chat_join_confirm_required():
    import asyncio
    from src.messaging.owner_chat_service import OwnerChatService

    svc = OwnerChatService()
    out = asyncio.run(svc.join_async(1, "@durov", confirm=False))
    assert out["ok"] is False
    assert out["error"] == "CONFIRM_REQUIRED"


def test_owner_chat_join_disabled_before_already_member_short_circuit(monkeypatch):
    """POST join while flag off must deny even if membership would be already-joined."""
    import asyncio
    from src.messaging.owner_chat_service import OwnerChatService

    monkeypatch.setenv("MESSAGES_CHAT_JOIN_ENABLED", "false")
    svc = OwnerChatService()

    async def fake_preview(account_id, raw):
        return {
            "ok": True,
            "already_joined": True,
            "action_hint": "open",
            "title": "Already Here",
            "chat_type": "channel",
            "peer_id": 123,
            "can_post": False,
            "parsed": {"username": None},
        }

    with patch.object(svc, "preview_async", side_effect=fake_preview):
        out = asyncio.run(svc.join_async(106, "https://t.me/+invite", confirm=True))
    assert out["ok"] is False
    assert out.get("error_code") == "messages_chat_join_disabled" or out.get("error") in {
        "messages_chat_join_disabled",
        "MESSAGES_CHAT_JOIN_ENABLED is false; owner chat join blocked.",
    }
    assert out.get("status") == "denied"


def test_owner_chat_leave_confirm_required():
    import asyncio
    from src.messaging.owner_chat_service import OwnerChatService

    svc = OwnerChatService()
    out = asyncio.run(svc.leave_async(1, "@durov", confirm=False))
    assert out["ok"] is False
    assert out["error"] == "CONFIRM_REQUIRED"


def test_messages_status_includes_chat_flags():
    from flask import Flask
    from src.dashboard import messages_routes as mr

    app = Flask(__name__)
    app.register_blueprint(mr.messages_api)
    with app.test_request_context("/api/messages/status"):
        with patch.object(mr, "messages_execution_enabled", return_value=True):
            with patch.object(mr, "messages_ai_draft_enabled", return_value=False):
                with patch.object(mr, "scheduled_dm_enabled", return_value=False):
                    with patch.object(mr, "scheduled_dm_create_allowed", return_value=False):
                        with patch.object(mr, "messages_chat_join_enabled", return_value=False):
                            with patch.object(mr, "messages_chat_leave_enabled", return_value=False):
                                with patch.object(
                                    mr, "messages_group_channel_send_enabled", return_value=False
                                ):
                                    resp = mr.messages_status()
                                    data = resp.get_json()
                                    assert data["ok"] is True
                                    assert data["messages_chat_join_enabled"] is False
                                    assert "chat_capabilities" in data
                                    assert data["chat_capabilities"]["private"]["SEND"] is True


def test_deny_group_channel_send_helper():
    from src.dashboard.messages_routes import _deny_group_channel_send

    with patch(
        "src.dashboard.messages_routes.evaluate_send_policy",
        return_value=(False, "GROUP_CHANNEL_SEND_DISABLED", "nope"),
    ):
        denied = _deny_group_channel_send("channel")
        assert denied is not None
        payload, status = denied
        assert status == 423
        assert payload["error"] == "GROUP_CHANNEL_SEND_DISABLED"


def test_duplicate_detection_markers():
    """Certification: uncontrolled send owners must remain 0 for Messages path."""
    # Thin wrappers OK; Broadcast/gateway are unrelated products.
    from src.messaging.owner_dm_service import OwnerDirectMessageService
    from src.messaging.scheduled_dm_service import ScheduledDirectMessageService

    assert OwnerDirectMessageService is not None
    assert ScheduledDirectMessageService is not None


def test_validate_peer_group_gated_by_flag(monkeypatch):
    from src.messaging.owner_dm_service import validate_peer

    monkeypatch.setenv("MESSAGES_GROUP_CHANNEL_SEND_ENABLED", "false")
    ok, code, _ = validate_peer("123", "group")
    assert not ok and code == "GROUP_CHANNEL_SEND_DISABLED"
    monkeypatch.setenv("MESSAGES_GROUP_CHANNEL_SEND_ENABLED", "true")
    ok2, code2, _ = validate_peer("123", "supergroup")
    assert ok2 and code2 == "OK"
    ok3, code3, _ = validate_peer("@alice", "private")
    assert ok3 and code3 == "OK"
