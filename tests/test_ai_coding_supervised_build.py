"""Operator Mode + Supervised Build — static asset tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_supervised_build.js"
OM_JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_mode.js"
BUILDS_HTML = ROOT / "src/dashboard/templates/ai_coding_builds.html"


def test_operator_mode_js_exists() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "AiCodingOperatorMode" in text
    assert "Ready For Test" in text or "Ready For Manual Test" in text
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


def test_operator_mode_auto_fix_card() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderAutoFixCard" in text
    assert "Auto Fix" in text
    assert "auto_fix_confidence_label" in text
    assert "renderAutoFixAdvancedDetails" in text
    assert "Auto-fix diagnostics" in text
    routes = (ROOT / "src/dashboard/ai_coding_routes.py").read_text(encoding="utf-8")
    assert "/build-runs/<run_id>/auto-fix" in routes


def test_operator_mode_auto_fix_rendering() -> None:
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
const fixingRun = {{
  auto_fix_status: 'fixing',
  auto_fix_attempt_number: 1,
  auto_fix_max_attempts: 3,
  auto_fix_confidence_label: 'High',
  auto_fix_current_issue: 'ImportError',
  auto_fix_next_action: 'Re-running validation',
}};
const blockedRun = {{
  auto_fix_status: 'blocked',
  auto_fix_block_reason: 'Maximum fix attempts reached (3)',
}};
const container = {{ innerHTML: '' }};
OM.renderOperatorSimplePanel(container, fixingRun, {{}});
const fixingHtml = container.innerHTML;
container.innerHTML = '';
OM.renderOperatorSimplePanel(container, blockedRun, {{}});
const blockedHtml = container.innerHTML;
console.log(JSON.stringify({{
  fixingHasCard: fixingHtml.includes('op-auto-fix-panel'),
  fixingHasConfidence: fixingHtml.includes('High'),
  fixingHasIssue: fixingHtml.includes('ImportError'),
  blockedHasReason: blockedHtml.includes('Maximum fix attempts'),
  advancedHidden: !fixingHtml.includes('Auto-fix diagnostics'),
  advancedHasHistory: OM.renderAutoFixAdvancedDetails({{
    auto_fix_attempts: [{{ attempt_number: 1, failure_classification: 'ImportError', confidence_score: 82, auto_fix_status: 'retrying' }}],
    auto_fix_patch_summary: 'Added package markers',
  }}).includes('Retry history'),
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
    assert data["fixingHasCard"] is True
    assert data["fixingHasConfidence"] is True
    assert data["fixingHasIssue"] is True
    assert data["blockedHasReason"] is True
    assert data["advancedHidden"] is True
    assert data["advancedHasHistory"] is True


def test_operator_mode_execution_health_card() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderExecutionHealthCard" in text
    assert "Execution Health" in text
    assert "execution_failure_classification" in text
    assert "renderExecutionAdvancedDetails" in text
    assert "Execution diagnostics" in text
    routes = (ROOT / "src/dashboard/ai_coding_routes.py").read_text(encoding="utf-8")
    assert "/build-runs/<run_id>/execute" in routes


def test_operator_mode_execution_health_rendering() -> None:
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
const healthyRun = {{
  execution_status: 'healthy',
  execution_duration_sec: 2.5,
  execution_command_count: 1,
  execution_artifact_count: 2,
  execution_next_action: 'Continue workflow',
}};
const blockedRun = {{
  execution_status: 'blocked',
  execution_blocker_reason: 'Command not allowed: curl',
}};
const container = {{ innerHTML: '' }};
OM.renderOperatorSimplePanel(container, healthyRun, {{}});
const healthyHtml = container.innerHTML;
container.innerHTML = '';
OM.renderOperatorSimplePanel(container, blockedRun, {{}});
const blockedHtml = container.innerHTML;
console.log(JSON.stringify({{
  healthyHasCard: healthyHtml.includes('op-execution-health-panel'),
  blockedHasReason: blockedHtml.includes('Command not allowed'),
  advancedHiddenByDefault: !healthyHtml.includes('Execution diagnostics'),
  advancedHasLogs: OM.renderExecutionAdvancedDetails({{
    execution_report_path: '/tmp/report.json',
    execution_commands: [{{ argv: ['pwd'], exit_code: 0, stdout_excerpt: 'ok' }}],
  }}).includes('Execution diagnostics'),
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
    assert data["healthyHasCard"] is True
    assert data["blockedHasReason"] is True
    assert data["advancedHiddenByDefault"] is True
    assert data["advancedHasLogs"] is True


def test_operator_mode_workspace_safety_card() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderWorkspaceSafetyCard" in text
    assert "Workspace Safety" in text
    assert "Clean isolated workspace" in text
    assert "Source repo had unrelated changes" in text
    assert "workspace_dirty_source_detected" in text
    assert "renderWorkspaceAdvancedDetails" in text
    assert "Workspace cleanup" in text
    assert "workspace_cleanup_command" in text
    routes = (ROOT / "src/dashboard/ai_coding_routes.py").read_text(encoding="utf-8")
    assert "/build-runs/<run_id>/workspace/prepare" in routes


def test_operator_mode_workspace_card_rendering() -> None:
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
const readyRun = {{
  workspace_status: 'ready',
  workspace_branch: 'ai-build/demo/abc12345',
  workspace_base_commit: 'abc1234567890abcdef',
  workspace_dirty_source_detected: false,
}};
const dirtyRun = Object.assign({{}}, readyRun, {{
  workspace_dirty_source_detected: true,
  workspace_dirty_source_summary: '2 untracked',
}});
const blockedRun = {{
  workspace_status: 'blocked',
  workspace_blocker_reason: 'Worktree path already exists',
}};
const container = {{ innerHTML: '' }};
OM.renderOperatorSimplePanel(container, readyRun, {{}});
const readyHtml = container.innerHTML;
container.innerHTML = '';
OM.renderOperatorSimplePanel(container, dirtyRun, {{}});
const dirtyHtml = container.innerHTML;
container.innerHTML = '';
OM.renderOperatorSimplePanel(container, blockedRun, {{}});
const blockedHtml = container.innerHTML;
console.log(JSON.stringify({{
  readyHasCard: readyHtml.includes('op-workspace-safety-panel'),
  dirtyHasWarning: dirtyHtml.includes('unrelated changes'),
  blockedHasReason: blockedHtml.includes('Worktree path already exists'),
  cleanupOnlyAdvanced: OM.renderWorkspaceAdvancedDetails({{
    workspace_cleanup_command: 'git worktree remove',
  }}).includes('git worktree remove'),
  simpleHasCleanup: readyHtml.includes('git worktree remove'),
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
    assert data["readyHasCard"] is True
    assert data["dirtyHasWarning"] is True
    assert data["blockedHasReason"] is True
    assert data["cleanupOnlyAdvanced"] is True
    assert data["simpleHasCleanup"] is False


def test_operator_mode_production_release_card() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderProductionReleaseCard" in text
    assert "Release</div>" in text or 'op-release-card-title">Release' in text
    assert "Prepare Release" in text
    assert "Request Release Approval" in text
    assert "Run Dry Run" in text
    assert "Release locked by safety settings" in text
    assert "bindReleaseActions" in text
    assert "fetchReleaseStatus" in text
    assert "resolveReleaseReadiness" in text


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
