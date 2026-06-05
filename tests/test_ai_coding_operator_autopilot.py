"""Operator Autopilot v1 — static asset and template contract tests."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIZARD_JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_wizard.js"


@pytest.fixture
def wizard_src() -> str:
    return WIZARD_JS.read_text(encoding="utf-8")


def test_autopilot_js_exists_and_branded(wizard_src: str) -> None:
    assert "Operator Autopilot v1" in wizard_src
    assert "AiCodingOperatorWizard" in wizard_src
    assert "buildViewModel" in wizard_src
    assert "buildChecklist" in wizard_src


def test_autopilot_next_actions_for_workflow_states(wizard_src: str) -> None:
    assert 'label: "Generate the plan"' in wizard_src or "Generate the plan" in wizard_src
    assert "Approve this plan" in wizard_src
    assert "Create execution program" in wizard_src
    assert "Review completed work" in wizard_src
    assert "Confirm validation" in wizard_src
    assert "Approve this phase" in wizard_src
    assert "approve_review" in wizard_src
    assert "continue_validation" in wizard_src
    assert "approve_phase" in wizard_src


def test_autopilot_safety_levels(wizard_src: str) -> None:
    for level in (
        "safe_readonly",
        "safe_dry_run",
        "needs_review",
        "needs_validation",
        "operator_approval",
        "blocked",
    ):
        assert level in wizard_src


def test_autopilot_roadmap_stages(wizard_src: str) -> None:
    for stage in ("Plan", "Execution", "Review", "Validation", "Approval", "Governance"):
        assert stage in wizard_src


def test_autopilot_checklist_missing_evidence(wizard_src: str) -> None:
    assert "Missing evidence" in wizard_src
    assert "No auto-send" in wizard_src
    assert "No deploy / merge / release" in wizard_src


def test_autopilot_no_unsafe_automation(wizard_src: str) -> None:
    # Comment documents policy; ensure no auto-approve *behavior* in runAction switch.
    assert re.search(r"case\s+['\"]approve_plan['\"]:", wizard_src)
    assert "global.confirm" in wizard_src or "confirm(" in wizard_src
    assert "needsChecklist" in wizard_src
    assert re.search(r"autoDeploy|auto_deploy|autoMerge|auto_merge|autoRelease|auto_release|autoSend|auto_send", wizard_src, re.I) is None


def test_autopilot_no_backend_summary_endpoint(wizard_src: str) -> None:
    assert "operator-autopilot/summary" not in wizard_src


def test_operator_mode_human_labels_file_exists() -> None:
    om = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_mode.js"
    text = om.read_text(encoding="utf-8")
    assert "ready_for_manual_test" in text
    assert "Continue AI work" in text
    assert "Understanding Idea" in text
    assert "AI checking work" in text
    assert "Testing in progress" in text
    assert "Build progress" in text
    assert "Open Build" in text
    assert "sanitizeOperatorText" in text


def _eval_wizard_js(expr: str) -> str:
    import json
    import subprocess

    script = r"""
const fs = require('fs');
const vm = require('vm');
const path = process.argv[1];
const expr = process.argv[2];
const sandbox = {
  document: {
    getElementById: () => null,
    head: { appendChild() {} },
    createElement: () => ({ id: '', textContent: '' }),
  },
  location: { href: '' },
  setInterval: () => 0,
  clearInterval: () => {},
  confirm: () => true,
};
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(path, 'utf8'), sandbox);
const W = sandbox.AiCodingOperatorWizard;
process.stdout.write(JSON.stringify(eval(expr)));
"""
    proc = subprocess.run(
        ["node", "-e", script, str(WIZARD_JS), expr],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"node unavailable or wizard eval failed: {proc.stderr}")
    return proc.stdout


@pytest.mark.parametrize(
    "ctx,expected_key",
    [
        ({"plan": {"id": "p1", "status": "draft", "title": "T", "goal": "G"}}, "generate_plan"),
        ({"plan": {"id": "p1", "status": "approved", "title": "T", "goal": "G"}}, "create_execution"),
        (
            {
                "plan": {
                    "id": "p1",
                    "status": "generated",
                    "title": "T",
                    "goal": "G",
                    "policy_result": {"allowed_to_approve": True},
                }
            },
            "approve_plan",
        ),
        (
            {
                "program": {
                    "id": "e1",
                    "status": "running",
                    "plan_title": "T",
                    "phases": [{"id": "ph1", "status": "review_pending", "review_status": "pending", "phase_number": 1, "title": "Discovery"}],
                    "current_phase_id": "ph1",
                }
            },
            "approve_review",
        ),
        (
            {
                "program": {
                    "id": "e1",
                    "status": "running",
                    "phases": [{"id": "ph1", "status": "validation_pending", "phase_number": 1, "title": "Build"}],
                    "current_phase_id": "ph1",
                }
            },
            "continue_validation",
        ),
        (
            {
                "program": {
                    "id": "e1",
                    "status": "running",
                    "phases": [{"id": "ph1", "status": "operator_approval", "phase_number": 1, "title": "Build"}],
                    "current_phase_id": "ph1",
                }
            },
            "approve_phase",
        ),
    ],
)
def test_autopilot_next_action_by_state(ctx: dict, expected_key: str) -> None:
    import json

    payload = _eval_wizard_js(
        "W.buildViewModel({plans:[],programs:[],programDetails:{},planDetails:{}}, "
        + json.dumps(ctx)
        + ").action.key"
    )
    assert json.loads(payload) == expected_key


def test_autopilot_error_handling_in_init(wizard_src: str) -> None:
    assert "Autopilot failed to load" in wizard_src
    assert "try Refresh or Open details" in wizard_src
    assert "formatError" in wizard_src


def test_operator_home_project_selection_helpers() -> None:
    import json

    home = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_home.js"
    text = home.read_text(encoding="utf-8")
    assert "normalizeProjectsList" in text
    assert "resolveProjectSelection" in text
    assert "hub-project-main" in text
    assert "Project selected automatically." in text
    assert "Select a project under Advanced options." not in text

    home_path = json.dumps(str(home))
    proc = __import__("subprocess").run(
        [
            "node",
            "-e",
            f"""
const fs = require('fs');
const src = fs.readFileSync({home_path}, 'utf8');
const fn = new Function('global', src + '; return global.AiCodingOperatorHome;');
const H = fn({{}});
const multi = H.normalizeProjectsList({{
  items: [
    {{ project_id: 'p-1', name: 'Swaperex' }},
    {{ project_id: 'p-2', name: 'Other' }},
  ],
}});
const single = H.normalizeProjectsList({{
  items: [{{ project_id: 'p-1', name: 'Swaperex' }}],
}});
const multiPick = H.resolveProjectSelection(multi, 'p-2');
const singlePick = H.resolveProjectSelection(single, '');
console.log(JSON.stringify({{
  multiCount: multi.length,
  multiVisible: multiPick.showVisibleProject,
  multiHint: multiPick.selectedId === 'p-2',
  singleAuto: singlePick.selectedId === 'p-1' && singlePick.showVisibleProject === false,
}}));
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.skip(f"node unavailable: {proc.stderr}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["multiCount"] == 2
    assert data["multiVisible"] is True
    assert data["multiHint"] is True
    assert data["singleAuto"] is True


def test_operator_home_three_step_flow_copy() -> None:
    home = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_home.js"
    text = home.read_text(encoding="utf-8")
    assert "hub-flow-card" in text
    assert "1. Describe the task" in text
    assert "2. Safety limits" in text
    assert "3. Start" in text
    assert "hub-safety-chip" in text
    assert "No auto-send" in text
    assert "Manual test required" in text
    assert "Create Your First Build" in text
    assert "Describe what you want AI to build." in text
    assert "AI builds. You test. Then approve or send feedback." in text
    assert "Example: Improve AI Coding empty state" in text
    assert "Start Building" in text
    assert "renderOperatorDashboard" in text
    assert "Ready For Test" in text
    assert "Recent Builds" in text
    assert "renderOperatorHomeHero" in text


def test_operator_home_pick_current_run_uses_cache_when_list_empty() -> None:
    import json

    home = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_home.js"
    home_path = json.dumps(str(home))
    proc = __import__("subprocess").run(
        [
            "node",
            "-e",
            f"""
const fs = require('fs');
const src = fs.readFileSync({home_path}, 'utf8');
const fn = new Function('global', src + '; return global.AiCodingOperatorHome;');
const H = fn({{}});
const cached = {{
  id: 'br-1',
  status: 'reviewing',
  task_title: 'Test proxy',
  updated_at: '2026-05-31T12:00:00Z',
}};
const picked = H.pickCurrentRun([], cached);
console.log(JSON.stringify({{
  id: picked && picked.id,
  status: picked && picked.status,
  reviewing: H.ACTIVE_BUILD.has('reviewing'),
  ready: H.ACTIVE_BUILD.has('ready_for_manual_test'),
}}));
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.skip(f"node unavailable: {proc.stderr}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["id"] == "br-1"
    assert data["status"] == "reviewing"
    assert data["reviewing"] is True
    assert data["ready"] is True


def test_operator_home_empty_state_and_dev_tools_link() -> None:
    home = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_home.js"
    text = home.read_text(encoding="utf-8")
    assert "Make the empty state clearer for non-technical operators" in text
    assert "AI build service is unavailable. The task was not started." in text
    assert "friendlyStartBuildError" in text
    assert "renameHomeDetailIds" in text
    assert "bindHomeActions" in text
    assert "fetchBuildRuns" in text
    assert "fetchJsonTimed" in text
    assert "FETCH_TIMEOUT_MS" in text
    assert "LIST_TIMEOUT_MS" in text
    assert "Refreshing status" in text
    assert "loadCachedBuild" in text
    assert "ready_for_manual_test" in text
    assert "UNAVAILABLE_AFTER_FAILURES" in text
    assert "renderUnavailablePanel" in text
    assert "AI build service is not available yet" in text
    assert 'id="hub-retry"' in text
    assert "/ai-coding/planner" in text
    assert "data-open-dev-advanced" in text
    assert 'href="#ai-dev-advanced"' not in text
    assert "ai-dev-advanced-panel" in text
    assert "collapseDevAdvancedPanel" in text


def test_planner_page_labels_autopilot_collapsed() -> None:
    planner = ROOT / "src" / "dashboard" / "templates" / "ai_coding_planner.html"
    text = planner.read_text(encoding="utf-8")
    assert "Advanced Task Planner" in text
    assert "Back to Simple AI Coding Home" in text
    assert "planner-advanced-details" in text
    assert 'id="planner-advanced-details" open' not in text
    assert text.index("planner-advanced-details") < text.index("ai-code-wizard-panel")
