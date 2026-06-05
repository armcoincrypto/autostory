"""Operator Mode + Supervised Build — static asset tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_supervised_build.js"
OM_JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_mode.js"
BUILDS_HTML = ROOT / "src/dashboard/templates/ai_coding_builds.html"


def test_operator_mode_js_exists() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "AiCodingOperatorMode" in text
    assert "Ready For Test" in text
    assert "Continue AI work" in text
    assert "Build progress" in text
    assert "renderProductionReadinessCard" in text
    assert "renderWhatToTestCard" in text
    assert "Manual test package" in text
    assert "Production checklist" in text
    assert "renderProductionChecklistPanel" in text
    assert "renderOperatorSimplePanel" in text
    assert "bindManualTestPackageActions" in text
    assert "op-mtp-summary" in text
    assert "isActiveNonTerminalStatus" in text
    assert "safe_to_continue" in text
    assert "sanitizeOperatorText" in text
    assert "operator-continue" not in text
    assert "friendlyOperatorError" in text


def test_supervised_build_operator_mode() -> None:
    text = JS.read_text(encoding="utf-8")
    assert "Operator Mode" in text
    assert "renderOperatorSimplePanel" in text or "renderDetail" in text
    om = OM_JS.read_text(encoding="utf-8")
    assert "op-advanced-details" in om
    assert "data-operator-mode" in om
    assert "Ready for manual test" in text
    assert "operator-continue" in text
    assert "sb-primary" in text
    assert "sb-decline-feedback" in text
    assert "Send feedback" in text
    assert "Next action:" not in text
    assert "validation_passed" not in text
    assert "renderOperatorSimplePanel" in text
    assert "renderProductionReadinessCard" in om
    assert "Manual test package" in om
    assert "Manual Test Passed" in om
    assert "Needs Fixes" in om
    assert "mtp-approve" in om
    assert "op-mtp-checklist" in om
    assert "op-mtp-summary" in om
    assert "op-manual-test-approved" in om
    assert "renderProductionChecklistPanel" in om
    assert "Status:" not in text
    assert "Manual test checklist" not in text


def test_supervised_build_normalizes_projects_list() -> None:
    text = JS.read_text(encoding="utf-8")
    assert "normalizeProjectsList" in text
    assert "projectLabelOf" in text
    assert "Array.isArray(data.items)" in text


def test_supervised_build_no_unsafe_actions() -> None:
    text = JS.read_text(encoding="utf-8")
    assert "autoDeploy" not in text
    assert "auto_merge" not in text
    assert "/auto-send" not in text


def test_builds_page_operator_mode() -> None:
    html = BUILDS_HTML.read_text(encoding="utf-8")
    assert "ai_coding_operator_mode.js" in html
    assert "Advanced / Developer Tools" in html
    assert "ai-sb-detail" in html
    assert JS.read_text(encoding="utf-8")
    assert "What should AI build" in JS.read_text(encoding="utf-8")


def test_operator_mode_production_release_card() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderProductionReleaseCard" in text
    assert "Production release" in text
    assert "Prepare release plan" in text
    assert "Approve production release" in text
    assert "Run approved release" in text
    assert "Dry run required" in text
    assert "Production release is not automatic" in text
    assert "bindReleaseActions" in text
    assert "fetchReleaseStatus" in text


def test_pre_manual_test_feedback_uses_feedback_endpoint() -> None:
    home = (ROOT / "src/dashboard/static/js/ai_coding_operator_home.js").read_text(encoding="utf-8")
    sb = JS.read_text(encoding="utf-8")
    om = OM_JS.read_text(encoding="utf-8")
    assert "submitFeedback" in home
    assert "submitFeedback" in sb
    assert "/feedback" in home
    assert "/feedback" in sb
    assert "isManualTestFeedbackStatus" in om
    assert "Send notes to AI" in om
    assert "Needs Fixes" in om
    assert "bindManualTestPackageActions" in om
    assert 'source: \'operator_pre_manual_test\'' in home or "operator_pre_manual_test" in home
    routes = (ROOT / "src/dashboard/ai_coding_routes.py").read_text(encoding="utf-8")
    assert "/build-runs/<run_id>/feedback" in routes


def test_active_draft_build_at_zero_percent_shows_continue_ai_work() -> None:
    """Regression: active non-terminal builds must always expose Continue AI work."""
    import json
    import subprocess

    om_path = json.dumps(str(OM_JS))
    proc = subprocess.run(
        [
            "node",
            "-e",
            f"""
const fs = require('fs');
const vm = require('vm');
const path = {om_path};
const sandbox = {{
  document: {{
    head: {{ appendChild() {{}} }},
    createElement: () => ({{ id: '', textContent: '' }}),
  }},
}};
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(path, 'utf8'), sandbox);
const OM = sandbox.AiCodingOperatorMode;
const run = {{
  id: 'f4a2148f-f483-4d2d-95a1-948ff962a1c1',
  status: 'draft',
  task_title: 'Improve AI Coding empty state',
  production_readiness_percent: 0,
  production_status_label: 'AI is working',
  primary_action_label: 'Continue AI work',
  safe_to_continue: true,
  manual_test_required: false,
}};
const action = OM.primaryAction(run);
const missingLabelRun = Object.assign({{}}, run, {{ primary_action_label: undefined }});
const staleDoneRun = Object.assign({{}}, run, {{ primary_action_label: 'Done' }});
const container = {{ innerHTML: '' }};
OM.renderOperatorSimplePanel(container, run, {{}});
console.log(JSON.stringify({{
  show: action.show,
  id: action.id,
  label: action.label,
  missingLabel: OM.primaryAction(missingLabelRun).label,
  staleDoneShow: OM.primaryAction(staleDoneRun).show,
  htmlHasPrimary: container.innerHTML.includes('id="sb-primary"'),
  htmlHasContinue: container.innerHTML.includes('Continue AI work'),
  active: OM.isActiveNonTerminalStatus('draft'),
}}));
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        import pytest

        pytest.skip(f"node unavailable: {proc.stderr}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["show"] is True
    assert data["id"] == "advance"
    assert data["label"] == "Continue AI work"
    assert data["missingLabel"] == "Continue AI work"
    assert data["staleDoneShow"] is True
    assert data["htmlHasPrimary"] is True
    assert data["htmlHasContinue"] is True
    assert data["active"] is True
