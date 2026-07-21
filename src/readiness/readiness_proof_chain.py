"""
P9.25 — Proof-aware readiness qualification (read-only filesystem + metadata).

Uses P9.23/P9.24 live auth reports and P9.24 install manifests. No Telethon network.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from src.core.telethon_schema import TELETHON_COMPATIBLE_SCHEMA_VERSION

PROOF_STATUS_AUTH_OK = "AUTH_OK"
REASON_AUTH_OK_NOT_ENABLED = "live_auth_ok_scheduler_not_enabled"

LIVE_AUTH_PROOF_GLOBS = (
    "p9_43_live_auth_proof_*.json",
    "p9_24_live_auth_proof_*.json",
    "p9_23_live_auth_proof_*.json",
)
INSTALL_MANIFEST_GLOBS = (
    "p9_43_install_manifest_*.json",
    "p9_24_install_manifest_*.json",
)
INSTALL_MANIFEST_GLOB = "p9_24_install_manifest_*.json"  # backward compat
DEFAULT_LAB_ROOT = Path("data/recovery_lab")


def sha256_file(path: Path) -> str:
    """Hash a local proof artifact without depending on recovery tooling."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_reports(
    reports_dir: Path,
    glob_patterns: tuple[str, ...],
) -> list[tuple[str, dict[str, Any]]]:
    if not reports_dir.is_dir():
        return []
    loaded: list[tuple[str, dict[str, Any]]] = []
    paths: list[Path] = []
    for pattern in glob_patterns:
        paths.extend(reports_dir.glob(pattern))
    for path in sorted(paths, key=lambda p: p.name, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            loaded.append((path.name, data))
    return loaded


def find_latest_live_auth_proof(
    account_id: int,
    lab_root: Path | None = None,
) -> Optional[dict[str, Any]]:
    """Latest AUTH_OK live auth proof (P9.24 production preferred over P9.23 converted)."""
    root = lab_root or DEFAULT_LAB_ROOT
    aid = int(account_id)
    candidates: list[tuple[int, str, dict[str, Any]]] = []

    for name, data in _load_json_reports(root / "reports", LIVE_AUTH_PROOF_GLOBS):
        if int(data.get("account_id", -1)) != aid:
            continue
        if data.get("proof_status") != PROOF_STATUS_AUTH_OK:
            continue
        is_production = (
            data.get("proof_source") == "production"
            or data.get("phase") in ("P9.24", "P9.43")
            or "source_production_path" in data
        )
        rank = 3 if data.get("phase") == "P9.43" else (2 if is_production else 1)
        candidates.append((rank, name, data))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


def find_latest_install_manifest(
    account_id: int,
    lab_root: Path | None = None,
) -> Optional[dict[str, Any]]:
    """Latest install manifest (P9.43 preferred) with install_status INSTALLED."""
    root = lab_root or DEFAULT_LAB_ROOT
    aid = int(account_id)
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for name, data in _load_json_reports(root / "reports", INSTALL_MANIFEST_GLOBS):
        if int(data.get("account_id", -1)) != aid:
            continue
        if data.get("install_status") != "INSTALLED":
            continue
        rank = 3 if data.get("phase") == "P9.43" else 2
        candidates.append((rank, name, data))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


def _production_sha_from_meta(meta: dict[str, Any]) -> Optional[str]:
    path = meta.get("session_path")
    if not path:
        return None
    p = Path(str(path))
    if not p.is_file():
        return None
    return sha256_file(p)


def evaluate_auth_ok_not_enabled(
    meta: dict[str, Any],
    lab_root: Path | None = None,
) -> Optional[dict[str, Any]]:
    """
  Return qualification details when account meets AUTH_OK_NOT_ENABLED criteria.

  Conservative: fleet tier, session on disk, schema v7, resolver clear, AUTH_OK proof,
  production unchanged during proof, production hash matches install manifest or proof.
  """
    if not meta.get("found"):
        return None

    tier = (meta.get("tier") or "").strip().lower()
    if tier != "fleet":
        return None
    if meta.get("is_reserved") or meta.get("is_controller"):
        return None
    if not meta.get("session_exists"):
        return None

    schema_version = meta.get("schema_version")
    if schema_version is None or int(schema_version) != TELETHON_COMPATIBLE_SCHEMA_VERSION:
        return None

    resolver = (meta.get("resolver_code") or "").strip() or None
    if resolver:
        return None

    proof = find_latest_live_auth_proof(int(meta["account_id"]), lab_root)
    if proof is None:
        return None
    if not proof.get("production_unchanged"):
        return None

    prod_sha = _production_sha_from_meta(meta)
    if not prod_sha:
        return None

    manifest = find_latest_install_manifest(int(meta["account_id"]), lab_root)
    proof_source = proof.get("proof_source") or (
        "production" if proof.get("phase") == "P9.24" else "converted"
    )

    if proof_source == "production" or proof.get("phase") == "P9.24":
        proof_sha = proof.get("production_sha_before") or proof.get("production_sha_after")
        if not proof_sha or proof_sha != prod_sha:
            return None
    else:
        if manifest is None:
            return None
        installed_sha = manifest.get("production_sha256_after")
        if not installed_sha or installed_sha != prod_sha:
            return None

    if manifest is not None:
        installed_sha = manifest.get("production_sha256_after")
        if installed_sha and installed_sha != prod_sha:
            return None

    return {
        "account_id": int(meta["account_id"]),
        "proof_phase": proof.get("phase"),
        "proof_source": proof_source,
        "production_sha256": prod_sha,
        "install_manifest_present": manifest is not None,
        "reason": REASON_AUTH_OK_NOT_ENABLED,
    }


def is_stale_v1_legacy_error(meta: dict[str, Any]) -> bool:
    """True when v1 still reports legacy_sqlite but operational session matches compatible schema."""
    fc = (meta.get("readiness_failure_code") or "").strip()
    if fc != "legacy_sqlite_session_format":
        return False
    try:
        return int(meta.get("schema_version") or 0) == TELETHON_COMPATIBLE_SCHEMA_VERSION
    except (TypeError, ValueError):
        return False
