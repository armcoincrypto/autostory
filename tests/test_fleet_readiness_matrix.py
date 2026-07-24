"""Focused tests for fleet readiness matrix + remediation safety boundary."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.stories.fleet_certification import normalize_classification
from src.stories.fleet_readiness_matrix import (
    EXCLUDED_ROLES,
    PUBLISHING_ROLES,
    build_canonical_matrix,
    classify_operational_role,
    operator_action_for,
)


FORBIDDEN_IMPORT_FRAGMENTS = (
    "controlled_live_run",
    "story_publisher",
    "publisher.story",
)
FORBIDDEN_NAMES = {
    "SendStoryRequest",
    "EditStoryRequest",
    "DeleteStoriesRequest",
    "send_message",
    "send_file",
    "controlled_live_run",
}


def test_normalize_maps_legacy_auth_ok():
    assert normalize_classification("AUTH_OK") == "READY_FOR_SEPARATE_CONTROLLED_CANARY"
    assert normalize_classification("CERTIFIED_PUBLISH") == "CERTIFIED_PUBLISH"


def test_role_classification_excludes_protected_and_ai():
    certified = {140, 106}
    protected = SimpleNamespace(
        id=206, status=SimpleNamespace(value="active"), purpose="both"
    )
    ai = SimpleNamespace(id=113, status=SimpleNamespace(value="active"), purpose="both")
    hold = SimpleNamespace(id=34, status=SimpleNamespace(value="active"), purpose="disabled")
    pub = SimpleNamespace(id=107, status=SimpleNamespace(value="active"), purpose="both")
    cert = SimpleNamespace(id=140, status=SimpleNamespace(value="active"), purpose="both")
    assert classify_operational_role(protected, certified) == "PROTECTED_CONTROLLER"
    assert classify_operational_role(ai, certified) == "AI_OR_INFRASTRUCTURE_RESERVED"
    assert classify_operational_role(hold, certified) == "INTENTIONALLY_DISABLED"
    assert classify_operational_role(pub, certified) == "PUBLISHING_ACCOUNT"
    assert classify_operational_role(cert, certified) == "CERTIFIED_REFERENCE"
    assert "PROTECTED_CONTROLLER" in EXCLUDED_ROLES
    assert "PUBLISHING_ACCOUNT" in PUBLISHING_ROLES


def test_operator_action_for_auth_failed_is_specific():
    action = operator_action_for("AUTH_FAILED", "PUBLISHING_ACCOUNT")
    assert action is not None
    assert "OTP" in action
    assert "2FA" in action
    assert "login alone" in action


def test_matrix_inventory_completeness_and_sorting():
    accounts = [
        SimpleNamespace(
            id=20,
            status=SimpleNamespace(value="active"),
            purpose="both",
            first_name="B",
            last_name=None,
            username=None,
            user_id=2,
            phone_number="+15550000020",
            stories_today=0,
            stories_today_on=None,
        ),
        SimpleNamespace(
            id=10,
            status=SimpleNamespace(value="active"),
            purpose="both",
            first_name="A",
            last_name=None,
            username=None,
            user_id=1,
            phone_number="+15550000010",
            stories_today=0,
            stories_today_on=None,
        ),
    ]
    audit = {
        "audit_run_id": "fleet-certification-fixture",
        "audit_completed_at": "2026-07-24T00:00:00Z",
        "accounts": [
            {
                "account_id": 10,
                "display_name": "A",
                "configured_enabled": True,
                "configured_status": "active",
                "purpose": "both",
                "expected_telegram_user_id": 1,
                "expected_username": None,
                "phone_redacted": "+15***0010",
                "session_type": "string",
                "session_present": True,
                "session_readable": True,
                "auth_valid": True,
                "telegram_user_id": 1,
                "telegram_username": None,
                "identity_matches": True,
                "story_api_available": True,
                "story_probe_status": "allowed",
                "controlled_publish_certified": False,
                "last_auth_at": "2026-07-24T00:00:00Z",
                "last_story_at": None,
                "classification": "AUTH_OK",
                "reason_codes": ["no_certified_story_evidence"],
                "safe_error_summary": None,
                "audit_run_id": "fleet-certification-fixture",
            },
            {
                "account_id": 20,
                "display_name": "B",
                "configured_enabled": True,
                "configured_status": "active",
                "purpose": "both",
                "expected_telegram_user_id": 2,
                "expected_username": None,
                "phone_redacted": "+15***0020",
                "session_type": "string",
                "session_present": True,
                "session_readable": True,
                "auth_valid": False,
                "telegram_user_id": None,
                "telegram_username": None,
                "identity_matches": False,
                "story_api_available": False,
                "story_probe_status": "not_run",
                "controlled_publish_certified": False,
                "last_auth_at": None,
                "last_story_at": None,
                "classification": "AUTH_FAILED",
                "reason_codes": ["telegram_unauthorized"],
                "safe_error_summary": "Telegram session is not authorized",
                "audit_run_id": "fleet-certification-fixture",
            },
        ],
    }

    db = MagicMock()
    db.query.return_value.order_by.return_value.all.return_value = accounts
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = False

    with patch("src.stories.fleet_readiness_matrix.get_db_context", return_value=ctx), patch(
        "src.stories.fleet_readiness_matrix.durable_certification_evidence", return_value={}
    ), patch(
        "src.stories.fleet_readiness_matrix.inspect_session",
        return_value=SimpleNamespace(kind="string", present=True, readable=True),
    ), patch(
        "src.stories.fleet_readiness_matrix.stories_today_effective", return_value=0
    ), patch(
        "src.stories.fleet_readiness_matrix.production_story_day", return_value="2026-07-24"
    ):
        matrix = build_canonical_matrix(audit)

    assert matrix["totals"]["total_configured_accounts"] == 2
    assert [r["account_id"] for r in matrix["accounts"]] == [10, 20]
    assert matrix["accounts"][0]["classification"] == "READY_FOR_SEPARATE_CONTROLLED_CANARY"
    assert matrix["accounts"][1]["classification"] == "AUTH_FAILED"
    assert matrix["accounts"][1]["required_operator_action"]
    assert matrix["publish_controls"] is False
    assert "session_string" not in str(matrix)


def _assert_module_has_no_publish_surface(relative_path: str) -> None:
    source_path = Path(relative_path)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported)
    for fragment in FORBIDDEN_IMPORT_FRAGMENTS:
        assert fragment not in joined
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert called.isdisjoint(FORBIDDEN_NAMES)
    assert "STORY_EXECUTION_ENABLED" not in source or "os.environ[" not in source
    assert "CONTROLLED_STORY_EXECUTION_ENABLED" not in source or "os.environ[" not in source


def test_remediation_modules_cannot_publish_or_enable_execution():
    _assert_module_has_no_publish_surface("src/stories/fleet_certification.py")
    _assert_module_has_no_publish_surface("src/stories/fleet_readiness_matrix.py")
    _assert_module_has_no_publish_surface("src/dashboard/fleet_readiness_routes.py")
