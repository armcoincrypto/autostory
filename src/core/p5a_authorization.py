"""P5A scoped repeatability authorization manifest and validation."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

MANIFEST_PATH = Path("data/audit/p5a_scope_manifest.json")
MARKER = "__p5a_certification__"
PILOT_ACCOUNT = 107
TARGET_USERNAME = "cryptodiscussing"
DEFAULT_TTL_MINUTES = 30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def message_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_message_body(ts: Optional[datetime] = None) -> str:
    ts = ts or _utc_now()
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    return f"P5A controlled repeatability test {stamp}. No action required."


def load_manifest() -> Optional[dict[str, Any]]:
    if not MANIFEST_PATH.is_file():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_manifest(manifest: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def create_manifest(
    *,
    message_body: str,
    target_id: int,
    target_tg_id: Optional[int] = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> dict[str, Any]:
    now = _utc_now()
    manifest = {
        "phase": "P5A",
        "authorization_id": str(uuid.uuid4()),
        "created_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=ttl_minutes)).isoformat(),
        "account_id": PILOT_ACCOUNT,
        "target_identifier": TARGET_USERNAME,
        "target_id": int(target_id),
        "target_tg_id": int(target_tg_id) if target_tg_id else None,
        "message_body": message_body,
        "expected_message_sha256": message_sha256(message_body),
        "max_live_sends": 1,
        "max_jobs": 1,
        "max_deliveries": 1,
        "max_gateway_jobs": 1,
        "allow_retry": False,
        "allow_join": False,
        "allow_story": False,
        "allow_campaign": False,
        "allow_discovery": False,
        "operator_approved": True,
        "job_id": None,
        "armed": False,
        "consumed": False,
        "scope": "scoped_single_account_repeatability",
        "fresh_job": True,
        "job_marker": MARKER,
    }
    save_manifest(manifest)
    return manifest


def p5a_single_send_enabled() -> bool:
    return os.environ.get("P5A_SINGLE_SEND_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def validate_p5a_authorization(
    *,
    account_id: Optional[int] = None,
    target_id: Optional[int] = None,
    job_marker: Optional[str] = None,
    job_id: Optional[int] = None,
    require_armed: bool = False,
    allow_consumed: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    """Return (ok, reason_code, audit)."""
    audit: dict[str, Any] = {}
    if str(job_marker or "").strip() != MARKER:
        return False, "p5a_job_not_fresh", {"job_marker": job_marker}

    manifest = load_manifest()
    if not manifest:
        return False, "p5a_scope_inactive", audit

    audit["authorization_id"] = manifest.get("authorization_id")
    if manifest.get("consumed") and not allow_consumed:
        return False, "p5a_authorization_consumed", audit

    if manifest.get("reserved") and not allow_consumed:
        if job_id is None or int(manifest.get("job_id") or 0) != int(job_id):
            return False, "p5a_authorization_consumed", audit

    try:
        expires = datetime.fromisoformat(str(manifest["expires_at_utc"]).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if _utc_now() > expires:
            return False, "p5a_authorization_expired", audit
    except (KeyError, ValueError, TypeError):
        return False, "p5a_scope_inactive", audit

    if require_armed and not manifest.get("armed"):
        return False, "p5a_scope_inactive", audit

    if int(manifest.get("account_id") or 0) != PILOT_ACCOUNT:
        return False, "p5a_account_mismatch", audit
    if account_id is not None and int(account_id) != PILOT_ACCOUNT:
        return False, "p5a_account_mismatch", audit

    if target_id is not None and int(manifest.get("target_id") or 0) != int(target_id):
        return False, "p5a_target_mismatch", audit

    if job_id is not None and manifest.get("job_id") is not None:
        if int(manifest["job_id"]) != int(job_id):
            return False, "p5a_job_not_fresh", audit

    if not p5a_single_send_enabled() and require_armed:
        return False, "p5a_scope_inactive", audit

    try:
        from src.core.p5a_send_counter import p5a_live_send_remaining

        remaining = p5a_live_send_remaining()
    except Exception as exc:
        return False, "p5a_counter_limit_reached", {"error": str(exc)}
    if remaining <= 0 and not allow_consumed:
        return False, "p5a_counter_limit_reached", {**audit, "p5a_remaining": remaining}

    audit["p5a_remaining_before"] = remaining
    return True, "p5a_scoped_repeatability_authorized", audit


def reserve_authorization(*, job_id: int) -> tuple[bool, str]:
    """Atomically reserve authorization before external send (no retry after reserve)."""
    manifest = load_manifest()
    if not manifest:
        return False, "p5a_scope_inactive"
    if manifest.get("consumed"):
        return False, "p5a_authorization_consumed"
    if manifest.get("reserved") and int(manifest.get("job_id") or 0) != int(job_id):
        return False, "p5a_authorization_consumed"
    if manifest.get("job_id") and int(manifest["job_id"]) != int(job_id):
        return False, "p5a_job_not_fresh"
    manifest["reserved"] = True
    manifest["reserved_at_utc"] = _utc_now().isoformat()
    manifest["job_id"] = int(job_id)
    save_manifest(manifest)
    return True, "reserved"


def mark_consumed() -> None:
    manifest = load_manifest()
    if not manifest:
        return
    manifest["consumed"] = True
    manifest["consumed_at_utc"] = _utc_now().isoformat()
    save_manifest(manifest)


def set_armed(armed: bool = True) -> None:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError("No P5A manifest to arm")
    manifest["armed"] = bool(armed)
    manifest["armed_at_utc"] = _utc_now().isoformat() if armed else None
    save_manifest(manifest)


def set_job_id(job_id: int) -> None:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError("No P5A manifest")
    manifest["job_id"] = int(job_id)
    save_manifest(manifest)


def relock_manifest() -> None:
    manifest = load_manifest()
    if not manifest:
        return
    manifest["armed"] = False
    manifest["relocked_at_utc"] = _utc_now().isoformat()
    save_manifest(manifest)
