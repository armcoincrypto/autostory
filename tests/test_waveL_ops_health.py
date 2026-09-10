"""Wave L — ops health / observability contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_ops_health_module_no_live_probes():
    src = (ROOT / "src/ops/ops_health.py").read_text(encoding="utf-8")
    assert "TelegramClient" not in src
    assert "api.openai.com" not in src
    assert "send_message" not in src
    assert "LIVE_TELEGRAM" not in src or "live_telegram_probes" in src


def test_ai_draft_metrics_no_prompt_fields(tmp_path, monkeypatch):
    from src.ops import ai_draft_metrics as m

    path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(m, "DEFAULT_METRICS_PATH", path)
    m.record_ai_draft_metric(
        ok=True,
        model="gpt-test",
        latency_ms=12,
        input_tokens=10,
        output_tokens=5,
    )
    m.record_ai_draft_metric(ok=False, error_code="AI_DRAFT_TIMEOUT", latency_ms=100)
    m.record_ai_draft_metric(ok=False, error_code="AI_DRAFT_PROVIDER_ERROR", latency_ms=20)
    m.record_ai_draft_metric(ok=False, error_code="AI_DRAFT_PROVIDER_ERROR", latency_ms=20)
    rows = m.load_ai_draft_metrics(path=path)
    assert len(rows) == 4
    blob = path.read_text(encoding="utf-8")
    assert "prompt" not in blob.lower()
    assert "conversation" not in blob.lower()
    assert "draft" not in blob  # field name absent
    summary = m.summarize_ai_draft_metrics(rows)
    assert summary["prompts_persisted"] is False
    assert summary["failure_spike"] is True
    assert summary["success_today"] == 1


def test_policy_disabled_products(monkeypatch):
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "false")
    monkeypatch.setenv("BROADCAST_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("MESSAGES_AI_DRAFT_ENABLED", "true")
    from src.ops.ops_health import _check_broadcast, _check_scheduled_dm

    sd = _check_scheduled_dm()
    bc = _check_broadcast()
    assert sd.detail and sd.detail.get("state") == "OK_POLICY_DISABLED"
    assert bc.detail and bc.detail.get("state") == "OK_POLICY_DISABLED"
    assert sd.state == "info"
    assert bc.state == "info"


def test_fleet_matrix_age_states(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import src.ops.ops_health as oh

    latest = tmp_path / "latest.json"
    refresh = tmp_path / "refresh_status.json"
    monkeypatch.setattr(oh, "FLEET_LATEST", latest)
    monkeypatch.setattr(oh, "FLEET_REFRESH", refresh)

    refresh.write_text(json.dumps({"error": None, "completed_at": "x", "counts": {"certified": 90}}), encoding="utf-8")

    def write_age(hours: float):
        gen = datetime.now(timezone.utc) - timedelta(hours=hours)
        latest.write_text(
            json.dumps(
                {
                    "generated_at": gen.isoformat().replace("+00:00", "Z"),
                    "totals": {"certified": 90, "auth_failed": 1, "check_required": 0},
                }
            ),
            encoding="utf-8",
        )

    write_age(1)
    checks = oh._check_fleet_matrix()
    by = {c.name: c for c in checks}
    assert by["fleet_matrix"].state == "healthy"

    write_age(19)
    checks = oh._check_fleet_matrix()
    by = {c.name: c for c in checks}
    assert by["fleet_matrix"].state == "warning"

    write_age(25)
    checks = oh._check_fleet_matrix()
    by = {c.name: c for c in checks}
    assert by["fleet_matrix"].state == "critical"

    refresh.write_text(json.dumps({"error": "boom", "counts": {}}), encoding="utf-8")
    write_age(1)
    checks = oh._check_fleet_matrix()
    by = {c.name: c for c in checks}
    assert by["fleet_matrix_refresh"].state == "critical"


def test_disk_thresholds(monkeypatch):
    import src.ops.ops_health as oh
    from types import SimpleNamespace

    monkeypatch.setattr(oh, "DISK_WARN", 80)
    monkeypatch.setattr(oh, "DISK_CRIT", 90)

    monkeypatch.setattr(oh.shutil, "disk_usage", lambda _p: SimpleNamespace(total=100, used=50, free=50))
    assert oh._check_disk().state == "healthy"
    monkeypatch.setattr(oh.shutil, "disk_usage", lambda _p: SimpleNamespace(total=100, used=85, free=15))
    assert oh._check_disk().state == "warning"
    monkeypatch.setattr(oh.shutil, "disk_usage", lambda _p: SimpleNamespace(total=100, used=95, free=5))
    assert oh._check_disk().state == "critical"


def test_alert_dedup():
    from src.ops.ops_health import compute_alert_events

    report1 = {
        "generated_at": "t1",
        "checks": [
            {"name": "web", "state": "critical", "summary": "web down"},
            {"name": "broadcast", "state": "info", "summary": "disabled"},
        ],
    }
    events, state = compute_alert_events(report1, previous_alert_state={})
    assert len(events) == 1 and events[0]["type"] == "new"
    events2, state2 = compute_alert_events(report1, previous_alert_state=state)
    assert events2 == []
    report_ok = {
        "generated_at": "t2",
        "checks": [{"name": "web", "state": "healthy", "summary": "ok"}],
    }
    events3, state3 = compute_alert_events(report_ok, previous_alert_state=state2)
    assert any(e["type"] == "recovered" for e in events3)
    assert state3.get("open") == {}


def test_messages_uncertain_fixture(tmp_path, monkeypatch):
    import sqlite3
    import src.ops.ops_health as oh

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE owner_dm_intents (id INTEGER PRIMARY KEY, status TEXT, created_at TEXT, updated_at TEXT)"
    )
    con.execute(
        "INSERT INTO owner_dm_intents(status, created_at, updated_at) VALUES ('UNCERTAIN','2026-01-01','2026-01-01')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(oh, "DB_PATH", db)
    checks = {c.name: c for c in oh._check_messages()}
    assert checks["messages_uncertain"].state == "critical"


def test_backup_stale(tmp_path, monkeypatch):
    import src.ops.ops_health as oh
    from datetime import datetime, timedelta, timezone

    path = tmp_path / "backup.json"
    monkeypatch.setattr(oh, "BACKUP_STATUS", path)
    monkeypatch.setattr(oh, "BACKUP_MAX_AGE_H", 36.0)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps({"state": "ok", "finished_at_utc": old}), encoding="utf-8")
    assert oh._check_backup().state == "critical"
    path.write_text(json.dumps({"state": "failed", "finished_at_utc": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    assert oh._check_backup().state == "critical"


def test_openai_provider_records_metric_not_prompt(tmp_path, monkeypatch):
    from src.messaging.openai_draft_provider import OpenAIHttpDraftProvider, MessageDraftRequest
    from src.ops import ai_draft_metrics as m

    path = tmp_path / "m.jsonl"
    monkeypatch.setattr(m, "DEFAULT_METRICS_PATH", path)
    provider = OpenAIHttpDraftProvider(api_key="", model="x")
    out = provider.generate(
        MessageDraftRequest(system_prompt="SECRET_PROMPT", conversation_blocks=[{"role": "user", "text": "private"}])
    )
    assert out.ok is False
    blob = path.read_text(encoding="utf-8")
    assert "SECRET_PROMPT" not in blob
    assert "private" not in blob


def test_advanced_page_has_ops_health():
    text = (ROOT / "src/dashboard/templates/advanced.html").read_text(encoding="utf-8")
    assert "Ops health" in text
    assert "/api/ops-health" in text


def test_ops_health_api_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "waveL-ops-token")
    monkeypatch.setenv("AI_AGENT_AUTO_LOOP_ENABLED", "false")
    monkeypatch.setenv("BROADCAST_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "false")
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    assert client.get("/api/ops-health").status_code == 401
    r = client.get("/api/ops-health", headers={"X-Admin-Token": "waveL-ops-token"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["contracts"]["live_telegram_probes"] == 0
    assert body["contracts"]["openai_probes"] == 0
    assert body["contracts"]["monitor_can_send_customer_dm"] is False
    names = {c["name"] for c in body["checks"]}
    assert "broadcast" in names
    assert "scheduled_dm" in names
