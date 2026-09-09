"""Wave C — off-host backup / DR script contracts."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wave_c_backup_scripts_exist_and_fail_closed_markers():
    backup = (ROOT / "scripts/ops/storyfleet_offhost_backup.sh").read_text(encoding="utf-8")
    assert "OFFSITE_BACKUP_ENCRYPTION_KEY" in backup
    assert "sqlite3" in backup
    assert ".backup" in backup
    assert "telegram-session-keys.env" in backup
    assert "sessions.key" in backup
    assert "gpg" in backup
    assert "rsync" in backup
    assert "offhost_backup_status.json" in backup

    restore = (ROOT / "scripts/ops/storyfleet_offhost_restore_drill.sh").read_text(encoding="utf-8")
    assert "integrity_check" in restore
    assert "telegram_live_connect=SKIPPED_BY_DESIGN" in restore
    assert "sessions.key" in restore

    age = (ROOT / "scripts/ops/storyfleet_backup_age_check.sh").read_text(encoding="utf-8")
    assert "STORYFLEET_BACKUP_AGE_OK" in age
    assert "STORYFLEET_BACKUP_AGE_FAIL" in age

    runbook = ROOT / "docs/runbooks/STORYFLEET_DISASTER_RECOVERY.md"
    assert runbook.is_file()
    text = runbook.read_text(encoding="utf-8")
    assert "OFF_HOST=YES" in text
    assert "ENCRYPTED=YES" in text


def test_wave_c_systemd_units_present():
    for name in (
        "storyfleet-offhost-backup.service",
        "storyfleet-offhost-backup.timer",
        "storyfleet-backup-age-check.service",
        "storyfleet-backup-age-check.timer",
    ):
        assert (ROOT / "deploy/systemd" / name).is_file()
