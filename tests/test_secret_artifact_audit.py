from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit" / "check_secret_artifacts.py"


def run_audit(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--json", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_insecure_secret_artifact_is_detected_without_value_output(tmp_path: Path) -> None:
    secret_value = "do-not-print-this-value"
    env = tmp_path / ".env"
    env.write_text(f"OPENAI_API_KEY={secret_value}\n", encoding="utf-8")
    env.chmod(0o644)

    result = run_audit(tmp_path)

    assert result.returncode == 2
    assert secret_value not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["insecure_permission_count"] == 1
    assert payload["artifacts"][0]["likely_secret_categories"] == ["openai", "provider_api"]


def test_fix_permissions_is_bounded_and_idempotent(tmp_path: Path) -> None:
    env = tmp_path / ".env.backup"
    env.write_text("TELEGRAM_API_HASH=sensitive\n", encoding="utf-8")
    env.chmod(0o664)

    first = run_audit(tmp_path, "--fix-permissions")
    second = run_audit(tmp_path, "--fix-permissions")

    assert first.returncode == 0
    assert second.returncode == 0
    assert env.stat().st_mode & 0o777 == 0o600
    assert json.loads(first.stdout)["permission_changes"] == 1
    assert json.loads(second.stdout)["permission_changes"] == 0


def test_example_file_remains_public_and_is_not_reported_insecure(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("OPENAI_API_KEY=replace-me\n", encoding="utf-8")
    example.chmod(0o644)

    result = run_audit(tmp_path, "--fix-permissions")

    assert result.returncode == 0
    assert example.stat().st_mode & 0o777 == 0o644
    payload = json.loads(result.stdout)
    assert payload["insecure_permission_count"] == 0
    assert payload["artifacts"][0]["is_example"] is True
