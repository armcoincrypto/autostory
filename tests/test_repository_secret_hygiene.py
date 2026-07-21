from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit" / "check_repository_secrets.py"


def run_scan(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )


def init_index(repo: Path, *files: str) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "--", *files], check=True)


def test_forbidden_tracked_backup_is_reported_without_value(tmp_path: Path) -> None:
    value = "never-print-this-secret"
    (tmp_path / ".env.backup").write_text(f"OPENAI_API_KEY={value}\n", encoding="utf-8")
    init_index(tmp_path, ".env.backup")

    result = run_scan(tmp_path)

    assert result.returncode == 2
    assert value not in result.stdout
    payload = json.loads(result.stdout)
    assert {row["rule_id"] for row in payload["findings"]} >= {"FORBIDDEN_TRACKED_PATH"}
    assert all(row["match"] == "[REDACTED]" for row in payload["findings"])


def test_high_confidence_literal_reports_line_and_category(tmp_path: Path) -> None:
    value = "sk-" + "A" * 32
    (tmp_path / "config.py").write_text(f'KEY = "{value}"\n', encoding="utf-8")
    init_index(tmp_path, "config.py")

    result = run_scan(tmp_path)

    assert result.returncode == 2
    assert value not in result.stdout
    finding = json.loads(result.stdout)["findings"][0]
    assert finding["file"] == "config.py"
    assert finding["line_number"] == 1
    assert finding["secret_category"] == "provider_api"


def test_placeholder_example_and_ordinary_code_pass(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(
        "OPENAI_API_KEY=replace-me\nDASHBOARD_SECRET_KEY=generate-a-secure-random-key-here\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("token = environment.get('TOKEN')\n", encoding="utf-8")
    init_index(tmp_path, ".env.example", "app.py")

    result = run_scan(tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout)["finding_count"] == 0
