"""P5 — Release Workflow UX frontend tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OM_JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_mode.js"
HOME_JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_home.js"

FORBIDDEN_SIMPLE_MODE = (
    "Deploy Now",
    "Auto Deploy",
    "Force Release",
    "Prepare release plan",
    "Run approved release",
    "Approve production release",
)


def test_release_card_operator_labels() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "Prepare Release" in text
    assert "Run Dry Run" in text
    assert "Request Release Approval" in text
    assert "View Release Package" in text
    assert "Release locked by safety settings" in text
    assert "release_readiness" in text or "resolveReleaseReadiness" in text


def test_release_card_no_dangerous_buttons_in_simple_mode() -> None:
    visible = OM_JS.read_text(encoding="utf-8")
    fn_start = visible.find("function renderProductionReleaseCard")
    fn_end = visible.find("function bindReleaseActions", fn_start)
    chunk = visible[fn_start:fn_end]
    for term in ("Deploy Now", "Auto Deploy", "Force Release"):
        assert term not in chunk, f"Dangerous release label in card: {term}"
    assert 'data-release-action="run">Run Dry Run' in chunk
    assert "Deploy Now" not in chunk


def test_release_package_and_safety_gates() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderReleasePackageSection" in text
    assert "Dry-run Package" in text
    assert "What would change" in text
    assert "Validation plan" in text
    assert "Deploy plan" in text
    assert "Rollback plan" in text
    assert "Safety gates" in text
    assert "Auto deploy disabled" in text or "safety_gates" in text


def test_dry_run_package_advanced_details_collapsed() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderDryRunPackageAdvancedDetails" in text
    assert "Dry-run package (advanced)" in text
    card_start = text.find("function renderProductionReleaseCard")
    card_end = text.find("function bindReleaseActions", card_start)
    card = text[card_start:card_end]
    assert "package_id" not in card.lower()
    assert "renderDryRunPackageAdvancedDetails" not in card


def test_controlled_release_lifecycle_ui() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderControlledReleaseSection" in text
    assert "Controlled Release" in text
    assert "Releasing" in text
    assert "Verifying" in text
    assert "Release Failed" in text
    assert "Rolled Back" in text
    assert "Execute Release" in text
    assert "executeControlledRelease" in text


def test_controlled_release_advanced_details_collapsed() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    card_start = text.find("function renderProductionReleaseCard")
    card_end = text.find("function bindReleaseActions", card_start)
    card = text[card_start:card_end]
    assert "release_run_id" not in card
    assert "renderControlledReleaseAdvancedDetails" not in card
    assert "renderControlledReleaseAdvancedDetails" in text


def test_background_activity_card() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "Background Activity" in text
    assert "renderBackgroundActivityCard" in text
    assert "backgroundJobStatusLabel" in text
    assert "Queued" in text
    assert "Running" in text
    assert "Retry scheduled" in text
    assert "Finished" in text
    assert "Blocked" in text
    assert "startBackgroundJobPolling" in text


def test_worker_health_advanced_only() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "fetchWorkerHealth" in text
    assert "Worker health (advanced)" in text
    card_start = text.find("function renderBackgroundActivityCard")
    card_end = text.find("function renderBackgroundActivityAdvancedDetails", card_start)
    card = text[card_start:card_end]
    assert "worker_mode" not in card
    assert "Worker health" not in card


def test_project_selector_and_health_home() -> None:
    home = HOME_JS.read_text(encoding="utf-8")
    assert "fetchProjectsPlatform" in home
    assert "renderProjectOverviewCard" in home
    assert "Health:" in home
    assert "Queued:" in home


def test_project_platform_advanced_hidden_from_simple() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderProjectPlatformAdvancedDetails" in text
    assert "Project platform (advanced)" in text
    card_start = text.find("function renderBackgroundActivityCard")
    card_end = text.find("function renderBackgroundActivityAdvancedDetails", card_start)
    card = text[card_start:card_end]
    assert "repository_path" not in card


def test_background_activity_advanced_hidden_from_simple() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    card_start = text.find("function renderBackgroundActivityCard")
    card_end = text.find("function renderBackgroundActivityAdvancedDetails", card_start)
    card = text[card_start:card_end]
    assert "job_id" not in card
    assert "idempotency_key" not in card


def test_home_ready_for_release_section() -> None:
    text = HOME_JS.read_text(encoding="utf-8")
    assert "Ready For Release" in text
    assert "isReadyForReleaseRow" in text
    assert "enrichReleaseSummaries" in text


def test_release_advanced_ids_not_in_simple_card() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    card_start = text.find("function renderProductionReleaseCard")
    card_end = text.find("function bindReleaseActions", card_start)
    card = text[card_start:card_end]
    assert "Release plan:" not in card
    assert "Release run:" not in card
    assert "renderReleaseAdvancedDetails" not in card
