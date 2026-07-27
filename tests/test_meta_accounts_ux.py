"""Accounts UX and overview Meta connection summary."""
from __future__ import annotations

from src.social_agent.models import SocialConnection
from src.social_agent.services import _build_alerts, _meta_connection_summary, _onboarding


def test_meta_summary_disconnected():
    summary = _meta_connection_summary([])
    assert summary["connected"] is False
    assert "not connected" in summary["summary"].lower()


def test_meta_summary_connected_healthy():
    row = SocialConnection(
        provider="meta",
        status="connected",
        health="CONNECTED",
        selected_page_id="111",
        selected_instagram_id="222",
        display_name="Exswaping",
    )
    summary = _meta_connection_summary([row])
    assert summary["connected"] is True
    assert summary["page_healthy"] is True
    assert summary["instagram_healthy"] is True
    assert "Instagram account healthy" in summary["summary"]


def test_alerts_credentials_missing():
    integ = {
        "meta": {"status": "CREDENTIALS_MISSING"},
        "telegram": {"story_mutations_enabled": False},
    }
    alerts = _build_alerts(integ, [])
    assert any("Meta credentials not configured" in a for a in alerts)
    assert not any("public-content" in a.lower() for a in alerts)
    steps = _onboarding(integ, [])
    assert any("Install Meta App ID" in s for s in steps)
    assert not any("Exswaping content access" in s for s in steps)


def test_accounts_template_has_onboarding_copy():
    text = open("src/dashboard/templates/social_agent/accounts.html", encoding="utf-8").read()
    assert "Connect Meta" in text
    assert "Not connected" in text
    assert "Instagram account is no longer linked" in text
    assert "Check connection" in text
    assert "Page administrator" in text
