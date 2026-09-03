"""Wave 3 — Fleet Readiness owner page consolidation contracts."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_owner_nav_hides_fleet_readiness():
    text = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/stories/fleet-readiness"' not in text
    assert "> Fleet Readiness<" not in text and ">Fleet Readiness<" not in text
    assert 'href="/accounts"' in text
    assert 'href="/stories"' in text


def test_fleet_readiness_html_route_redirects_to_accounts():
    src = (ROOT / "src/dashboard/fleet_readiness_routes.py").read_text(encoding="utf-8")
    assert 'redirect("/accounts?filter=needs_attention"' in src
    assert 'render_template(\n        "fleet_readiness.html"' not in src
    assert 'render_template("fleet_readiness.html"' not in src
    # JSON API preserved for backend consumers.
    assert '@fleet_readiness_bp.route("/api/stories/fleet-readiness")' in src
    assert "build_operator_account_views" in src


def test_accounts_accepts_filter_query_param():
    text = (ROOT / "src/dashboard/templates/accounts_main.html").read_text(encoding="utf-8")
    assert "URLSearchParams" in text
    assert "needs_attention" in text
    assert "requestedFilter" in text


def test_accounts_and_api_still_share_mapper():
    from src.dashboard import accounts_main_view, fleet_readiness_routes
    from src.stories import operator_account_presentation as mapper

    assert "build_operator_account_views" in Path(accounts_main_view.__file__).read_text(encoding="utf-8")
    assert "build_operator_account_views" in Path(fleet_readiness_routes.__file__).read_text(encoding="utf-8")
    assert callable(mapper.build_operator_account_views)


def test_fleet_readiness_backend_modules_untouched_for_publish():
    """Consolidation must not introduce publish surfaces into readiness modules."""
    for rel in (
        "src/stories/fleet_readiness_matrix.py",
        "src/dashboard/fleet_readiness_routes.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for banned in ("SendStoryRequest", "publish_story", "send_message("):
            assert banned not in text
