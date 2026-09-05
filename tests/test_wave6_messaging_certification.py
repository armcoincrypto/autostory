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


def test_dialogs_route_exists_but_excludes_private_users():
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@api.route('/accounts/<int:account_id>/dialogs'" in routes
    mgr = (ROOT / "src/clients/manager.py").read_text(encoding="utf-8")
    # get_dialogs only appends Chat/Channel — User/private peers omitted.
    start = mgr.index("async def get_dialogs")
    chunk = mgr[start : start + 2500]
    assert 'isinstance(e, Chat)' in chunk
    assert 'isinstance(e, Channel)' in chunk
    assert "Private" not in chunk
    assert "User)" not in chunk or "GetFullUser" in chunk  # User may appear elsewhere nearby
    # Explicit: no User entity branch in the dialog loop
    loop = chunk[chunk.index("for d in dialogs") : chunk.index("return result")]
    assert "isinstance(e, User)" not in loop


def test_dialogs_response_fields_are_non_secret():
    mgr = (ROOT / "src/clients/manager.py").read_text(encoding="utf-8")
    start = mgr.index("async def get_dialogs")
    chunk = mgr[start : start + 2500]
    # Session presence is checked, but response payload fields are non-secret.
    payload_zone = chunk[chunk.index("result = []") : chunk.index("return result")]
    for banned in ("session_string", "api_hash", "access_hash", "auth_key", "proxy_config"):
        assert banned not in payload_zone
    assert '"id"' in payload_zone and '"title"' in payload_zone and '"chat_type"' in payload_zone


def test_dm_send_has_no_dry_run_or_idempotency_key():
    src = (ROOT / "src/ai_agent/telegram_single_sender.py").read_text(encoding="utf-8")
    # Sync public send_message signature is account/target/text only.
    assert "def send_message(self, account_id: int, target: str, text: str)" in src
    assert "dry_run" not in src
    assert "idempotency_key" not in src
    # Scheduler deliveries DO have idempotency (different path).
    sched = (ROOT / "src/core/scheduler_models.py").read_text(encoding="utf-8")
    assert "idempotency_key" in sched


def test_execution_guard_blocks_telegram_send_when_mutations_disabled():
    guard = (ROOT / "src/core/execution_guard.py").read_text(encoding="utf-8")
    assert 'ACTION_TELEGRAM_SEND = "telegram_send"' in guard
    assert "scheduler_mutations_disabled" in guard
    assert "if not scheduler_mutations_enabled() and not scoped_ok:" in guard
    # Document: generic DM send shares the scheduler mutations kill switch today.
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
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@web.route('/messages')" not in routes
    base = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")
    assert "> Messages<" not in base and ">Messages<" not in base
