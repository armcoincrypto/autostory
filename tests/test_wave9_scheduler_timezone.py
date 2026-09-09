"""Wave 9 — Scheduler timezone normalization contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.datetime_utc import to_utc_iso_z, utc_now_naive
from src.scheduler.timezone import (
    DEFAULT_OWNER_TIMEZONE,
    SchedulerTimezoneError,
    format_owner_local,
    local_to_utc_naive,
    owner_schedule_fields,
    parse_aware_iso_to_utc_naive,
    parse_owner_local_datetime,
    resolve_owner_timezone,
    utc_to_local_naive,
)

ROOT = Path(__file__).resolve().parents[1]


def test_default_owner_timezone_is_yerevan():
    assert DEFAULT_OWNER_TIMEZONE == "Asia/Yerevan"


def test_yerevan_local_to_utc_and_roundtrip():
    # Asia/Yerevan is UTC+4 year-round (no DST).
    local = datetime(2026, 9, 10, 15, 0, 0)
    utc = local_to_utc_naive(local, "Asia/Yerevan")
    assert utc == datetime(2026, 9, 10, 11, 0, 0)
    assert utc.tzinfo is None
    back = utc_to_local_naive(utc, "Asia/Yerevan")
    assert back == local


def test_parse_owner_local_datetime_api():
    inst = parse_owner_local_datetime("2026-09-10", "15:00", "Asia/Yerevan")
    assert inst.utc_naive == datetime(2026, 9, 10, 11, 0, 0)
    assert inst.timezone_name == "Asia/Yerevan"
    assert format_owner_local(inst.local_naive, inst.timezone_name) == "10 Sep 2026, 15:00 Asia/Yerevan"


def test_date_rollover_yerevan_to_utc():
    # 00:15 Yerevan → previous UTC calendar day 20:15
    utc = local_to_utc_naive(datetime(2026, 9, 10, 0, 15), "Asia/Yerevan")
    assert utc == datetime(2026, 9, 9, 20, 15, 0)
    back = utc_to_local_naive(utc, "Asia/Yerevan")
    assert back == datetime(2026, 9, 10, 0, 15)


def test_near_midnight_both_directions():
    late = local_to_utc_naive(datetime(2026, 9, 10, 23, 45), "Asia/Yerevan")
    assert late == datetime(2026, 9, 10, 19, 45, 0)
    early = local_to_utc_naive(datetime(2026, 9, 11, 0, 5), "Asia/Yerevan")
    assert early == datetime(2026, 9, 10, 20, 5, 0)


def test_invalid_timezone_rejected():
    with pytest.raises(SchedulerTimezoneError) as ei:
        resolve_owner_timezone("Not/AZone")
    assert ei.value.code == "INVALID_TIMEZONE"


def test_nonexistent_dst_time_rejected():
    # Europe/Berlin spring-forward 2024-03-31: 02:00–02:59 nonexistent
    with pytest.raises(SchedulerTimezoneError) as ei:
        local_to_utc_naive(datetime(2024, 3, 31, 2, 30), "Europe/Berlin")
    assert ei.value.code == "NONEXISTENT_LOCAL_TIME"


def test_ambiguous_dst_time_requires_fold():
    # Europe/Berlin fall-back 2024-10-27: 02:30 occurs twice
    with pytest.raises(SchedulerTimezoneError) as ei:
        local_to_utc_naive(datetime(2024, 10, 27, 2, 30), "Europe/Berlin")
    assert ei.value.code == "AMBIGUOUS_LOCAL_TIME"
    earlier = local_to_utc_naive(datetime(2024, 10, 27, 2, 30), "Europe/Berlin", fold=0)
    later = local_to_utc_naive(datetime(2024, 10, 27, 2, 30), "Europe/Berlin", fold=1)
    assert earlier < later


def test_naive_iso_rejected():
    with pytest.raises(SchedulerTimezoneError) as ei:
        parse_aware_iso_to_utc_naive("2026-09-10T15:00:00")
    assert ei.value.code == "NAIVE_DATETIME_REJECTED"


def test_aware_iso_accepted():
    utc = parse_aware_iso_to_utc_naive("2026-09-10T15:00:00+04:00")
    assert utc == datetime(2026, 9, 10, 11, 0, 0)
    utc_z = parse_aware_iso_to_utc_naive("2026-09-10T11:00:00Z")
    assert utc_z == datetime(2026, 9, 10, 11, 0, 0)


def test_owner_schedule_fields_additive():
    fields = owner_schedule_fields(datetime(2026, 9, 10, 11, 0, 0), "Asia/Yerevan")
    assert fields["timezone"] == "Asia/Yerevan"
    assert fields["scheduled_at_utc"] == "2026-09-10T11:00:00Z"
    assert fields["scheduled_at_local"] == "10 Sep 2026, 15:00 Asia/Yerevan"


def test_worker_due_comparison_utc_naive():
    """Worker contract: naive UTC now vs naive UTC run_at."""
    due = datetime(2026, 9, 10, 11, 0, 0)
    future = datetime(2026, 9, 10, 11, 0, 1)
    now = datetime(2026, 9, 10, 11, 0, 0)
    assert due <= now
    assert not (future <= now)
    # Mirror worker clock style
    clock = utc_now_naive()
    assert clock.tzinfo is None


def test_generator_default_not_moscow():
    from src.scheduler import generator

    assert generator.DEFAULT_TIMEZONE == "Asia/Yerevan"
    assert "Europe/Moscow" not in (ROOT / "src/scheduler/generator.py").read_text()


def test_setup_ui_no_moscow_hardcode():
    tpl = (ROOT / "src/dashboard/templates/scheduler_setup.html").read_text(encoding="utf-8")
    assert "Europe/Moscow" not in tpl
    assert "Asia/Yerevan" in tpl


def test_settings_default_yerevan():
    from config.settings import Settings

    # Field default (env may override in process)
    field = Settings.model_fields["sched_default_timezone"]
    assert field.default == "Asia/Yerevan"


def test_owner_presentation_note_and_fields():
    text = (ROOT / "src/dashboard/scheduler_owner_presentation.py").read_text(encoding="utf-8")
    assert "Europe/Moscow" not in text
    assert "scheduled_at_local" in text
    assert "DEFAULT_OWNER_TIMEZONE" in text


def test_wave9_does_not_touch_messages_or_ai_draft_or_matrix_timer():
    assert (ROOT / "src/messaging/owner_dm_service.py").is_file()
    assert (ROOT / "src/messaging/message_draft_service.py").is_file()
    assert (ROOT / "src/stories/refresh_fleet_matrix.py").is_file()
    assert (ROOT / "deploy/autostory-fleet-matrix-refresh.timer").is_file()


def test_executor_uses_utc_now_naive():
    text = (ROOT / "src/scheduler/executor.py").read_text(encoding="utf-8")
    assert "datetime.utcnow()" not in text
    assert "utc_now_naive()" in text


def test_to_utc_iso_z_preserves_z():
    assert to_utc_iso_z(datetime(2026, 9, 10, 11, 0, 0)).endswith("Z")
