"""Wave 4 — owner navigation cleanup contracts (IA only)."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")


def _owner_nav_hrefs() -> set[str]:
    """Collect top-level sidebar hrefs from base.html (before Log out)."""
    nav_start = BASE.index('<nav class="nav flex-column">')
    nav_end = BASE.index("</nav>", nav_start)
    chunk = BASE[nav_start:nav_end]
    hrefs = set()
    for line in chunk.splitlines():
        if 'href="' not in line:
            continue
        # skip logout
        if 'href="/logout"' in line:
            continue
        start = line.index('href="') + 6
        end = line.index('"', start)
        hrefs.add(line[start:end])
    return hrefs


def test_owner_sidebar_target_products_only():
    hrefs = _owner_nav_hrefs()
    assert hrefs == {
        "/",
        "/accounts",
        "/stories",
        "/scheduler",
        "/broadcast",
        "/agents",
        "/advanced",
    }


def test_owner_nav_hides_misleading_products():
    for label in (
        "Campaigns",
        "Fleet Readiness",
        "Discovery",
        "Operator",
        "AI Coding",
        "Social Agent",
        "Dexpert",
        "AI Agent",
        "Messages",
    ):
        assert f"> {label}<" not in BASE and f">{label}<" not in BASE
    for path in (
        "/campaigns",
        "/stories/fleet-readiness",
        "/discovery",
        "/operator",
        "/ai-coding",
        "/social-agent",
        "/dexpert",
        "/ai-agent",
    ):
        assert f'href="{path}"' not in BASE


def test_advanced_landing_links_operator_tools():
    text = (ROOT / "src/dashboard/templates/advanced.html").read_text(encoding="utf-8")
    for path in ("/operator", "/discovery", "/ai-coding", "/dexpert"):
        assert f'href="{path}"' in text
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@web.route('/advanced')" in routes
    assert "advanced.html" in routes


def test_campaigns_is_retired_page_not_crud():
    text = (ROOT / "src/dashboard/templates/campaigns.html").read_text(encoding="utf-8")
    assert "Legacy / unused" in text
    assert "createCampaignModal" not in text
    assert "New Campaign" not in text
    assert 'href="/broadcast"' in text
    assert 'href="/scheduler"' in text
    # Model/API kept (LEGACY_KEEP_TEMPORARILY)
    models = (ROOT / "src/core/models.py").read_text(encoding="utf-8")
    assert 'class Campaign(Base):' in models
    assert '__tablename__ = "campaigns"' in models
    api = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@api.route('/campaigns', methods=['GET'])" in api


def test_dashboard_quick_actions_real_workflows():
    text = (ROOT / "src/dashboard/templates/index.html").read_text(encoding="utf-8")
    for path in ("/accounts", "/stories", "/scheduler", "/broadcast", "/agents", "/advanced"):
        assert f'href="{path}"' in text
    assert 'href="/campaigns"' not in text
    assert 'href="/stories/fleet-readiness"' not in text
    assert "Active Campaigns" not in text


def test_agents_hub_social_active_ai_inactive():
    agents_tpl = (ROOT / "src/dashboard/templates/social_agent/agents.html").read_text(
        encoding="utf-8"
    )
    assert "inactive" in agents_tpl
    assert "a.status == 'inactive'" in agents_tpl
    registry = (ROOT / "src/social_agent/registry.py").read_text(encoding="utf-8")
    assert 'agent_id="social_agent"' in registry
    assert 'route="/social-agent"' in registry
    assert 'status="active"' in registry
    assert 'agent_id="ai_agent"' in registry
    assert 'status="inactive"' in registry


def test_fleet_readiness_owner_page_still_absent():
    assert 'href="/stories/fleet-readiness"' not in BASE
    assert not (ROOT / "src/dashboard/templates/fleet_readiness.html").exists()
    src = (ROOT / "src/dashboard/fleet_readiness_routes.py").read_text(encoding="utf-8")
    assert 'redirect("/accounts?filter=needs_attention"' in src
    assert '@fleet_readiness_bp.route("/api/stories/fleet-readiness")' in src


def test_scheduler_broadcast_accounts_stories_routes_untouched_markers():
    """Wave 4 must not rewrite core product route handlers."""
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@web.route('/stories')" in routes
    assert "@web.route('/scheduler')" in routes
    assert (ROOT / "src/dashboard/templates/scheduler.html").exists()
    assert (ROOT / "src/dashboard/templates/stories.html").exists()
    assert (ROOT / "src/dashboard/broadcast_routes.py").exists()
    assert (ROOT / "src/dashboard/accounts_main_view.py").exists()
