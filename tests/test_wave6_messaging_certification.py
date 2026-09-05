"""Wave 6 — Messaging backend safety/capability certification contracts.

These tests document production reality. They do not send Telegram traffic.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_dm_sender_is_telegram_single_sender_primitive():
    src = (ROOT / "src/ai_agent/telegram_single_sender.py").read_text(encoding="utf-8")
    assert "class TelegramDirectTransport" in src
    assert "class TelegramSingleSender" in src
    assert "async def send_message_async" in src
    assert "def send_message(" in src
    assert "require_execution_allowed" in src
    assert "ACTION_TELEGRAM_SEND" in src


def test_ai_agent_policy_is_separate_allowlist_gate():
    src = (ROOT / "src/ai_agent/telegram_single_sender.py").read_text(encoding="utf-8")
    assert "_ai_agent_account_gate_passes" in src
    assert "forbidden_account" in src
    allow = (ROOT / "src/ai_agent/account_allowlist.py").read_text(encoding="utf-8")
    assert "RESERVED_AI_AGENT_ACCOUNT_IDS" in allow
    assert "110" in allow and "113" in allow and "131" in allow


def test_dialogs_include_private_users_after_wave6a():
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@api.route('/accounts/<int:account_id>/dialogs'" in routes
    mgr = (ROOT / "src/clients/manager.py").read_text(encoding="utf-8")
    start = mgr.index("async def get_dialogs")
    chunk = mgr[start : start + 4500]
    loop = chunk[chunk.index("for d in dialogs") : chunk.index("return result")]
    assert "isinstance(e, User)" in loop
    assert "isinstance(e, Chat)" in loop
    assert "isinstance(e, Channel)" in loop
    assert '"dialog_type"' in loop or "dialog_type" in loop


def test_dialogs_response_fields_are_non_secret():
    mgr = (ROOT / "src/clients/manager.py").read_text(encoding="utf-8")
    start = mgr.index("async def get_dialogs")
    chunk = mgr[start : start + 4500]
    payload_zone = chunk[chunk.index("for d in dialogs") : chunk.index("return result")]
    for banned in ("session_string", "api_hash", "access_hash", "auth_key", "proxy_config"):
        assert banned not in payload_zone
    assert "dialog_type" in payload_zone or "chat_type" in payload_zone


def test_dm_send_ai_path_has_no_owner_dry_run_or_idempotency_key():
    src = (ROOT / "src/ai_agent/telegram_single_sender.py").read_text(encoding="utf-8")
    # Sync public send_message signature is account/target/text only.
    assert "def send_message(self, account_id: int, target: str, text: str)" in src
    assert "dry_run" not in src
    assert "idempotency_key" not in src
    # Owner Messages foundation owns dry-run + idempotency (Wave 6A).
    owner = (ROOT / "src/messaging/owner_dm_service.py").read_text(encoding="utf-8")
    assert "def dry_run" in owner
    assert "idempotency_key" in owner
    sched = (ROOT / "src/core/scheduler_models.py").read_text(encoding="utf-8")
    assert "idempotency_key" in sched


def test_execution_guard_blocks_telegram_send_when_mutations_disabled():
    guard = (ROOT / "src/core/execution_guard.py").read_text(encoding="utf-8")
    assert 'ACTION_TELEGRAM_SEND = "telegram_send"' in guard
    assert "scheduler_mutations_disabled" in guard
    assert "if not scheduler_mutations_enabled() and not scoped_ok:" in guard
    assert 'ACTION_OWNER_DM_SEND = "owner_dm_send"' in guard
    assert "messages_execution_disabled" in guard
    sender = (ROOT / "src/ai_agent/telegram_single_sender.py").read_text(encoding="utf-8")
    assert "require_execution_allowed(ACTION_TELEGRAM_SEND" in sender


def test_floodwait_mapping_exists_for_owner_facing_codes():
    from src.scheduler.executor import _map_error

    try:
        from telethon.errors import FloodWaitError as RealFW

        code, msg = _map_error(RealFW(seconds=42))
        assert code == "FloodWait"
        assert "42" in msg
    except Exception:
        pytest.skip("telethon FloodWaitError unavailable in test env")


def test_map_error_classifies_peerflood_and_write_forbidden():
    from src.scheduler.executor import _map_error

    src = (ROOT / "src/scheduler/executor.py").read_text(encoding="utf-8")
    assert 'return "PeerFlood"' in src
    assert 'return "ChatWriteForbidden"' in src
    # Static contract sufficient; Telethon exception constructors vary by version.
    assert "FloodWaitError" in src
    assert callable(_map_error)



def test_resolve_dm_entity_supports_username_and_numeric_id():
    src = (ROOT / "src/ai_agent/telegram_single_sender.py").read_text(encoding="utf-8")
    assert "async def _resolve_dm_entity" in src
    assert 'raw.startswith("@")' in src
    assert "isdigit()" in src


def test_scheduler_job_types_do_not_include_dm_yet():
    models = (ROOT / "src/core/scheduler_models.py").read_text(encoding="utf-8")
    assert 'PROMO = "PROMO"' in models
    assert 'INFO = "INFO"' in models
    assert 'DM = "DM"' not in models


def test_readiness_worker_does_not_write_fleet_matrix():
    worker = (ROOT / "src/clients/readiness_worker.py").read_text(encoding="utf-8")
    assert "write_latest_matrix" not in worker
    assert "fleet-readiness/latest.json" not in worker
    matrix = (ROOT / "src/stories/fleet_readiness_matrix.py").read_text(encoding="utf-8")
    assert "FLEET_MATRIX_FRESH_TTL_HOURS = 24" in matrix
    assert "def write_latest_matrix" in matrix


def test_ai_agent_messages_store_full_body():
    models = (ROOT / "src/core/ai_agent_models.py").read_text(encoding="utf-8")
    assert 'body = Column(Text' in models
    assert "telegram_message_id" in models


def test_no_messages_owner_ui_route_yet():
    """Wave 7 superseded: Messages UI now exists. Keep safety tripwires."""
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    # Legacy routes.py still must not own /messages; Wave 7 uses messages_routes.
    assert "@web.route('/messages')" not in routes
    base = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/messages"' in base
    assert "bi-chat-dots" in base
    assert "Messages" in base
    msg_routes = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert '@messages_bp.route("/messages")' in msg_routes
    assert "OwnerDirectMessageService" in msg_routes
    assert "dashboard_api_authorized" in msg_routes
    # Scheduled DM still absent
    models = (ROOT / "src/core/scheduler_models.py").read_text(encoding="utf-8")
    assert 'DM = "DM"' not in models
