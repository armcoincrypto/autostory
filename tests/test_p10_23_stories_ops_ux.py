"""P10.23 Stories operations UX simplification regressions."""
from __future__ import annotations

TOKEN = "p10-23-test-token"


def _app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _headers() -> dict[str, str]:
    return {"X-Admin-Token": TOKEN}


def _template() -> str:
    return open("src/dashboard/templates/stories.html", encoding="utf-8").read()


def test_refresh_button_visible_top_right() -> None:
    text = _template()
    assert 'id="btn-refresh-readiness-top"' in text
    assert "Refresh" in text


def test_technical_diagnostics_collapsed_by_default() -> None:
    text = _template()
    assert 'id="story-advanced-diagnostics"' in text
    assert "<details" in text
    assert "Technical diagnostics" in text
    assert "Controlled live path:" not in text


def test_no_duplicate_governance_runtime_labels_in_main_ui() -> None:
    text = _template()
    assert "Governance:" not in text
    assert "Story auth:" not in text
    assert text.count('id="selected-account-readiness"') == 1


def test_account_picker_uses_operator_labels() -> None:
    text = _template()
    assert "operatorPickerLabel" in text
    assert "function operatorStatusForRow" in text


def test_negative_counts_clamped() -> None:
    text = _template()
    assert "function clampDisplayCount" in text
    build_fn = text.split("function buildStoryRunPayload")[1].split("function storyEscapeHtml")[0]
    assert "n > 0" in build_fn


def test_primary_actions_visible() -> None:
    text = _template()
    assert 'id="ops-primary-actions"' in text
    assert 'id="btn-refresh-account"' in text
    assert 'id="btn-dryrun"' in text
    assert 'id="btn-schedule"' in text
    assert "Start approved Story" in text
    assert "function invalidatePrecheck" in text
    assert "function renderOperatorGuidance" in text


def test_advanced_panel_toggle() -> None:
    text = _template()
    assert "function toggleOpsAdvanced" in text
    panel_chunk = text.split('id="ops-advanced-panel"')[1][:120]
    assert "d-none" in panel_chunk


def test_operational_summary_four_cards() -> None:
    text = _template()
    assert 'id="ops-ready-count"' in text
    assert 'id="ops-needs-refresh-count"' in text
    assert 'id="ops-blocked-count"' in text
    assert 'id="ops-protected-count"' in text
    assert "function operatorSummaryCounts" in text


def test_dry_run_triggers_readiness_refresh() -> None:
    text = _template()
    run_fn = text.split("async function runStoryPrecheck")[1].split("async function runSelectedAccountAuthProbe")[0]
    assert "await refreshStoryReadinessState()" in run_fn


def test_stories_page_loads_ops_console() -> None:
    text = _template()
    assert "Story Operations" in text
    assert "ops-ready-count" in text
    assert "Run Dry Run" in text
    assert "ops-next-step" in text
    assert "Step 1 — Choose destination" in text


def test_media_change_invalidates_stale_precheck() -> None:
    text = _template()
    set_fn = text.split("function setMediaSelection")[1].split("function clearMediaSelection")[0]
    assert "invalidatePrecheck" in set_fn
    assert "runStoryPrecheck(true, {quiet: true})" in set_fn
    init = text.split("DOMContentLoaded")[1]
    assert "await runStoryPrecheck(true, {quiet: true});" not in init.split("loadBlacklist")[0]