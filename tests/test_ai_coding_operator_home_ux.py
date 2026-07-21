"""P4 — Operator Home UX language and dashboard structure."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME_JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_home.js"
OM_JS = ROOT / "src" / "dashboard" / "static" / "js" / "ai_coding_operator_mode.js"
AI_CODING_HTML = ROOT / "src" / "dashboard" / "templates" / "ai_coding.html"

FORBIDDEN_OPERATOR_HOME = (
    "review_pending",
    "validation_pending",
    "governance",
    "execution program",
    "workflow state",
    "Review transparency",
    "Program overview",
    "Awaiting validation",
    "Pending review",
)


def _operator_visible_home_js() -> str:
    text = HOME_JS.read_text(encoding="utf-8")
    marker = "function renderOperatorDashboard"
    idx = text.find(marker)
    assert idx >= 0
    chunk = text[idx:]
    for fn in ("function renderStartCard", "function renderUnavailablePanel", "function renderTaskDetail"):
        end = chunk.find(fn)
        if end > 0:
            chunk = chunk[:end]
    return chunk


def test_operator_home_dashboard_has_no_workflow_engine_language() -> None:
    visible = _operator_visible_home_js()
    lower = visible.lower()
    for term in FORBIDDEN_OPERATOR_HOME:
        assert term.lower() not in lower, f"Operator home exposes workflow term: {term}"


def test_operator_home_dashboard_sections() -> None:
    text = HOME_JS.read_text(encoding="utf-8")
    assert "Ready For Test" in text or "Ready For Manual Test" in text
    assert "Recent Builds" in text
    assert "Blocked" in text
    assert "Failed" in text
    assert "Create Your First Build" in text
    assert "Start Building" in text
    assert "Workspace Safety" not in text
    assert "renderWorkspaceSafetyCard" not in text


def test_operator_mode_home_hero_exports() -> None:
    text = OM_JS.read_text(encoding="utf-8")
    assert "renderOperatorHomeHero" in text
    assert "Open Build" in text
    assert "AI checking work" in text
    assert "Testing in progress" in text
    assert "Planning" in text
    assert "AI Working" in text


def test_ai_coding_page_operator_area_not_advanced_noise() -> None:
    html = AI_CODING_HTML.read_text(encoding="utf-8")
    home_end = html.find('id="ai-dev-advanced-panel"')
    assert home_end > 0
    operator_area = html[:home_end]
    assert "Review transparency" not in operator_area
    assert "Operations Overview" not in operator_area
    assert "Idea → Build → Test → Approve → Release" in operator_area


def test_operator_home_start_build_uses_auto_pipeline() -> None:
    text = HOME_JS.read_text(encoding="utf-8")
    assert "auto_start: true" in text
    assert "hub-priority" in text


def test_operator_home_has_notification_inbox() -> None:
    text = HOME_JS.read_text(encoding="utf-8")
    assert "renderNotificationInbox" in text
    assert "Build ready" in text
    assert "Feedback applied" in text
    assert "ai-notification-inbox" in text
