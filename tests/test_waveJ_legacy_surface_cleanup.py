"""Wave J — legacy owner surface cleanup contracts."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")
ROUTES = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
REGISTRY = (ROOT / "src/social_agent/registry.py").read_text(encoding="utf-8")
SA_ROUTES = (ROOT / "src/dashboard/social_agent_routes.py").read_text(encoding="utf-8")


def test_primary_nav_target_set():
    nav_start = BASE.index('<nav class="nav flex-column">')
    nav_end = BASE.index("</nav>", nav_start)
    chunk = BASE[nav_start:nav_end]
    hrefs = []
    for line in chunk.splitlines():
        if 'href="' not in line:
            continue
        start = line.index('href="') + 6
        end = line.index('"', start)
        hrefs.append(line[start:end])
    assert hrefs == [
        "/",
        "/accounts",
        "/stories",
        "/messages",
        "/scheduler",
        "/broadcast",
        "/agents",
        "/advanced",
    ]
    assert "Log out" in chunk
    assert 'action="{{ url_for(\'auth.logout\') }}"' in chunk or "auth.logout" in chunk


def test_campaigns_write_apis_gone():
    assert "@api.route('/campaigns', methods=['POST'])" in ROUTES
    assert "Marketing Campaigns write API is retired" in ROUTES
    assert "return render_template('campaigns.html'), 410" in ROUTES
    assert "ai_agent_retired_page" in ROUTES
    # Write handlers must not mutate Campaign rows anymore.
    post_chunk = ROUTES.split("@api.route('/campaigns', methods=['POST'])", 1)[1]
    post_chunk = post_chunk.split("@api.route(", 1)[0]
    assert "db.add(campaign)" not in post_chunk
    assert "), 410" in post_chunk


def test_ai_agent_owner_route_retired_410():
    assert "@web.route('/ai-agent')" in ROUTES
    assert "ai_agent_retired_page" in ROUTES
    assert "retired_product.html" in ROUTES
    assert (ROOT / "src/dashboard/templates/retired_product.html").exists()


def test_agents_registry_no_duplicate_primary_products():
    assert 'agent_id="social_agent"' in REGISTRY
    assert 'agent_id="ai_agent"' in REGISTRY
    assert 'status="inactive"' in REGISTRY
    assert 'agent_id="broadcast"' not in REGISTRY
    assert 'agent_id="ai_coding"' not in REGISTRY
    assert 'route="/broadcast"' not in REGISTRY
    assert 'route="/ai-coding"' not in REGISTRY


def test_social_agent_inbox_label_not_messages():
    assert '"label": "Agent inbox"' in SA_ROUTES
    assert '{"id": "messages", "label": "Messages"' not in SA_ROUTES
    messages_tpl = (
        ROOT / "src/dashboard/templates/social_agent/messages.html"
    ).read_text(encoding="utf-8")
    assert "Agent inbox" in messages_tpl
    # Canonical owner Messages product must remain distinct.
    assert 'href="/messages"' in BASE


def test_advanced_has_operator_tools_only():
    text = (ROOT / "src/dashboard/templates/advanced.html").read_text(encoding="utf-8")
    for path in ("/operator", "/discovery", "/ai-coding", "/dexpert", "/scheduler/setup"):
        assert f'href="{path}"' in text
    assert 'href="/campaigns"' not in text
    assert 'href="/ai-agent"' not in text


def test_fleet_readiness_still_redirects():
    src = (ROOT / "src/dashboard/fleet_readiness_routes.py").read_text(encoding="utf-8")
    assert 'redirect("/accounts?filter=needs_attention"' in src
    assert not (ROOT / "src/dashboard/templates/fleet_readiness.html").exists()


def test_autostory_tick_not_registered():
    integ = (ROOT / "src/stories/scheduler_integration.py").read_text(encoding="utf-8")
    assert "maybe_tick_story_rotation" in integ or "AutoStory product retirement" in integ
    # Tick registration must remain removed.
    assert "register_auto_story" not in integ.lower() or "removed" in integ.lower()
    app = (ROOT / "src/dashboard/app.py").read_text(encoding="utf-8")
    assert "tick_due_auto_story_campaigns" not in app
