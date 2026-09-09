"""Wave 5 — Scheduler owner UX presentation contracts."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from src.dashboard.scheduler_owner_presentation import (
    STATUS_OWNER_LABELS,
    TYPE_OWNER_LABELS,
    format_display_utc,
    map_job_status,
    map_job_type,
    owner_short_reason,
    present_job,
)


ROOT = Path(__file__).resolve().parents[1]


def test_owner_nav_still_lists_scheduler_not_setup():
    text = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/scheduler"' in text
    assert 'href="/scheduler/setup"' not in text.split("</nav>")[0]
    assert "> Campaigns<" not in text and ">Campaigns<" not in text


def test_scheduler_owner_template_has_empty_upcoming_and_readonly_banner():
    text = (ROOT / "src/dashboard/templates/scheduler.html").read_text(encoding="utf-8")
    assert "No upcoming scheduled tasks." in text
    assert "read-only mode" in text
    assert "Recent activity" in text
    assert "Advanced schedule setup" in text
    assert 'href="/scheduler/setup"' in text
    # No create/edit campaign setup on owner page
    assert "Campaign Setup" not in text
    assert "createCampaignModal" not in text
    assert "simple-account" not in text


def test_scheduler_setup_preserved_at_dedicated_route():
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@web.route('/scheduler/setup')" in routes
    assert "scheduler_setup.html" in routes
    assert "build_owner_scheduler_view" in routes
    assert (ROOT / "src/dashboard/templates/scheduler_setup.html").exists()
    setup = (ROOT / "src/dashboard/templates/scheduler_setup.html").read_text(encoding="utf-8")
    assert "Scheduling changes are currently disabled" in setup
    assert "Campaign Setup" in setup  # technical UI retained


def test_status_and_type_owner_mapping():
    expected = {
        "PENDING": "Scheduled",
        "RUNNING": "In progress",
        "SENT": "Sent",
        "FAILED": "Failed",
        "SKIPPED": "Skipped",
        "CANCELLED": "Cancelled",
    }
    assert STATUS_OWNER_LABELS == expected
    for raw, label in expected.items():
        m = map_job_status(raw)
        assert m["label"] == label
        assert m["raw"] == raw
        assert m["known"] == "true"
    unk = map_job_status("WEIRD")
    assert unk["label"] == "Unknown status"
    assert unk["raw"] == "WEIRD"
    assert map_job_type("PROMO")["label"] == "Promotional message"
    assert map_job_type("INFO")["label"] == "Information message"
    assert TYPE_OWNER_LABELS["PROMO"] == "Promotional message"


def test_utc_display_does_not_shift_naive_instant():
    dt = datetime(2026, 7, 14, 22, 55, 44)
    assert format_display_utc(dt) == "2026-07-14 22:55 UTC"
    # Aware UTC same wall clock
    from datetime import timezone

    aware = datetime(2026, 7, 14, 22, 55, 44, tzinfo=timezone.utc)
    assert format_display_utc(aware) == "2026-07-14 22:55 UTC"


def test_owner_short_reason_hides_markers():
    assert owner_short_reason("__p6_4_certification__") == ""
    assert "safety guard" in owner_short_reason("execution_guard:scheduler_mutations_disabled").lower()
    assert owner_short_reason("Failed to connect to Telegram") == "Failed to connect to Telegram"


def test_present_job_shape_and_diagnostics():
    job = MagicMock()
    job.id = 10
    job.account_id = 107
    job.target_id = 1
    job.type = "PROMO"
    job.status = "FAILED"
    job.run_at = datetime(2026, 5, 19, 12, 27, 29)
    job.created_at = datetime(2026, 5, 19, 12, 0, 0)
    job.updated_at = datetime(2026, 5, 19, 12, 27, 29)
    job.attempts = 1
    job.last_error = "Failed to connect to Telegram"
    job.lease_until = None
    job.lease_owner = None
    job.template_id = 3

    row = present_job(
        job,
        accounts={
            107: {"username": "Krystal", "first_name": "K", "phone_number": "+100"}
        },
        targets={1: {"title": "Crypto Group", "username": "crypto", "chat_type": "group"}},
        deliveries={},
    )
    assert row["status_label"] == "Failed"
    assert row["type_label"] == "Promotional message"
    assert row["account_label"] == "@Krystal"
    assert row["target_label"] == "Crypto Group"
    # Wave 9: primary display is owner-local (default Asia/Yerevan = UTC+4)
    assert row["scheduled_at"] == "19 May 2026, 16:27 Asia/Yerevan"
    assert row["scheduled_at_utc"] == "2026-05-19T12:27:29Z"
    assert row["timezone"] == "Asia/Yerevan"
    assert row["short_reason"] == "Failed to connect to Telegram"
    assert "lease_owner" in row["diagnostics"]


def test_advanced_links_scheduler_setup():
    text = (ROOT / "src/dashboard/templates/advanced.html").read_text(encoding="utf-8")
    assert 'href="/scheduler/setup"' in text


def test_scheduler_api_mutation_guard_untouched():
    """Wave 5 must not weaken mutation guards or rewrite worker."""
    mut = (ROOT / "src/dashboard/scheduler_mutations.py").read_text(encoding="utf-8")
    assert "scheduler_mutations_enabled" in mut
    assert "SCHEDULER_MUTATIONS_ENABLED" in mut
    worker = (ROOT / "src/scheduler/worker.py").read_text(encoding="utf-8")
    assert "LOOP_INTERVAL_SEC = 45" in worker
    # Owner page source must not invoke Telethon on render path
    owner = (ROOT / "src/dashboard/scheduler_owner_presentation.py").read_text(encoding="utf-8")
    for banned in ("TelegramClient", "telethon", "join_targets", "SendMessage", "send_message"):
        assert banned not in owner
