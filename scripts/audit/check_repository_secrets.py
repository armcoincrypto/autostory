#!/usr/bin/env python3
"""Fail closed on tracked secret artifacts and high-confidence secret literals."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Iterable

FORBIDDEN_NAMES = (
    re.compile(r"(^|/)\.env($|[._-])", re.I),
    re.compile(r"(^|/).*env.*(?:bak|backup|copy|old)(?:$|[._-])", re.I),
    re.compile(r"(^|/)(?:credentials|service-account)\.json$", re.I),
    re.compile(r"\.(?:pem|key)$", re.I),
)
EXAMPLE_NAMES = {".env.example", ".env.sample", ".env.template"}
PLACEHOLDERS = re.compile(
    r"(?i)^(?:|replace[-_ ]?me|your[_-].*|example|placeholder|changeme|"
    r"generate-a-secure-random-key-here|x+|<.*>)$"
)
CONTENT_RULES = (
    ("PRIVATE_KEY", "private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("OPENAI_KEY", "provider_api", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("AWS_ACCESS_KEY", "provider_api", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", "provider_api", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("TELEGRAM_BOT_TOKEN", "telegram_bot", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
)
ENV_ASSIGNMENT = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|API_HASH)[A-Za-z0-9_]*)"
    r"\s*=\s*(.*?)\s*$",
    re.I,
)


def tracked_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return sorted(p.decode("utf-8", "replace") for p in result.stdout.split(b"\0") if p)


def forbidden_artifact(path: str) -> bool:
    if Path(path).name in EXAMPLE_NAMES:
        return False
    return any(rule.search(path) for rule in FORBIDDEN_NAMES)


def scan_file(repo: Path, relative: str) -> Iterable[dict[str, object]]:
    path = repo / relative
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line_number, line in enumerate(lines, 1):
        for rule_id, category, pattern in CONTENT_RULES:
            if pattern.search(line):
                yield {
                    "file": relative,
                    "line_number": line_number,
                    "secret_category": category,
                    "rule_id": rule_id,
                    "match": "[REDACTED]",
                }
        if Path(relative).name.startswith(".env"):
            match = ENV_ASSIGNMENT.match(line)
            if match and not PLACEHOLDERS.match(match.group(2).strip().strip("\"'")):
                yield {
                    "file": relative,
                    "line_number": line_number,
                    "secret_category": "environment_secret",
                    "rule_id": "ENV_SECRET_VALUE",
                    "match": "[REDACTED]",
                }


def scan(repo: Path) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    paths = tracked_files(repo)
    for relative in paths:
        if forbidden_artifact(relative):
            findings.append(
                {
                    "file": relative,
                    "line_number": 0,
                    "secret_category": "tracked_secret_artifact",
                    "rule_id": "FORBIDDEN_TRACKED_PATH",
                    "match": "[REDACTED]",
                }
            )
        findings.extend(scan_file(repo, relative))
    return {"tracked_files_scanned": len(paths), "finding_count": len(findings), "findings": findings}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = scan(args.repo.resolve())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"tracked_files_scanned={payload['tracked_files_scanned']} "
            f"findings={payload['finding_count']}"
        )
        for finding in payload["findings"]:
            print(
                f"{finding['file']}:{finding['line_number']}\t"
                f"{finding['secret_category']}\t{finding['rule_id']}\t[REDACTED]"
            )
    return 2 if payload["finding_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
