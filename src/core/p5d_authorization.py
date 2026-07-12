"""P5D gateway restart durability authorization manifest."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

MANIFEST_PATH = Path("data/audit/p5d_scope_manifest.json")
MARKER = "__p5d_certification__"
PILOT_ACCOUNT = 107
TARGET_ID = 14
BINDING_ID = 45
TELEGRAM_PEER_ID = 8000295303
DEFAULT_TTL_MINUTES = 45


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def message_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_message_body(*, scenario: str, ts: Optional[datetime] = None) -> str:
    ts = ts or _utc_now()
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    return (
        f"P5D gateway restart durability test scenario {scenario} {stamp}. "
        "No action required."
    )


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
    scenario: str,
    message_body: str,
    target_id: int = TARGET_ID,
    target_tg_id: Optional[int] = TELEGRAM_PEER_ID,
    binding_id: int = BINDING_ID,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> dict[str, Any]:
    now = _utc_now()
    manifest = {
        "phase": "P5D",
        "scenario": str(scenario).strip().upper(),
        "authorization_id": str(uuid.uuid4()),
        "created_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=ttl_minutes)).isoformat(),
        "account_id": PILOT_ACCOUNT,
        "profile_id": 97,
        "binding_id": int(binding_id),
        "target_id": int(target_id),
        "target_tg_id": int(target_tg_id) if target_tg_id else None,
        "message_body": message_body,
        "expected_message_sha256": message_sha256(message_body),
        "max_live_sends": 1,
        "operator_approved": True,
        "job_id": None,
        "gateway_job_id": None,
        "delivery_id": None,
        "armed": False,
        "consumed": False,
        "scope": "gateway_restart_durability",
        "job_marker": MARKER,
    }
    save_manifest(manifest)
    return manifest


def p5d_single_send_enabled() -> bool:
    return os.environ.get("P5D_SINGLE_SEND_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def p5d_gateway_claim_extra_account_ids() -> frozenset[int]:
    if not p5d_single_send_enabled():
        return frozenset()
    manifest = load_manifest()
    if manifest and manifest.get("armed") and not manifest.get("consumed"):
        return frozenset({PILOT_ACCOUNT})
    return frozenset()


def validate_p5d_authorization(
    *,
    account_id: Optional[int] = None,
    target_id: Optional[int] = None,
    job_marker: Optional[str] = None,
    job_id: Optional[int] = None,
    require_armed: bool = False,
    allow_consumed: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    audit: dict[str, Any] = {}
    if str(job_marker or "").strip() != MARKER:
        return False, "p5d_job_not_fresh", {"job_marker": job_marker}
    manifest = load_manifest()
    if not manifest:
        return False, "p5d_scope_inactive", audit
    audit["authorization_id"] = manifest.get("authorization_id")
    if manifest.get("consumed") and not allow_consumed:
        return False, "p5d_authorization_consumed", audit
    if manifest.get("reserved") and not allow_consumed:
        if job_id is None or int(manifest.get("job_id") or 0) != int(job_id):
            return False, "p5d_authorization_consumed", audit
    try:
        expires = datetime.fromisoformat(str(manifest["expires_at_utc"]).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if _utc_now() > expires:
            return False, "p5d_authorization_expired", audit
    except (KeyError, ValueError, TypeError):
        return False, "p5d_scope_inactive", audit
    if require_armed and not manifest.get("armed"):
        return False, "p5d_scope_inactive", audit
    if int(manifest.get("account_id") or 0) != PILOT_ACCOUNT:
        return False, "p5d_account_mismatch", audit
    if account_id is not None and int(account_id) != PILOT_ACCOUNT:
        return False, "p5d_account_mismatch", audit
    if target_id is not None and int(manifest.get("target_id") or 0) != int(target_id):
        return False, "p5d_target_mismatch", audit
    if job_id is not None and manifest.get("job_id") is not None:
        if int(manifest["job_id"]) != int(job_id):
            return False, "p5d_job_not_fresh", audit
    if not p5d_single_send_enabled() and require_armed:
        return False, "p5d_scope_inactive", audit
    try:
        from src.core.p5d_send_counter import p5d_live_send_remaining

        remaining = p5d_live_send_remaining()
    except Exception as exc:
        return False, "p5d_counter_limit_reached", {"error": str(exc)}
    if remaining <= 0 and not allow_consumed:
        return False, "p5d_counter_limit_reached", {**audit, "p5d_remaining": remaining}
    audit["p5d_remaining_before"] = remaining
    return True, "p5d_gateway_restart_authorized", audit


def reserve_authorization(*, job_id: int) -> tuple[bool, str]:
    manifest = load_manifest()
    if not manifest:
        return False, "p5d_scope_inactive"
    if manifest.get("consumed"):
        return False, "p5d_authorization_consumed"
    if manifest.get("reserved") and int(manifest.get("job_id") or 0) != int(job_id):
        return False, "p5d_authorization_consumed"
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
        raise RuntimeError("No P5D manifest to arm")
    manifest["armed"] = bool(armed)
    manifest["armed_at_utc"] = _utc_now().isoformat() if armed else None
    save_manifest(manifest)


def set_job_id(job_id: int) -> None:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError("No P5D manifest")
    manifest["job_id"] = int(job_id)
    save_manifest(manifest)


def set_gateway_job_id(gateway_job_id: int) -> None:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError("No P5D manifest")
    manifest["gateway_job_id"] = int(gateway_job_id)
    save_manifest(manifest)


def set_delivery_id(delivery_id: int) -> None:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError("No P5D manifest")
    manifest["delivery_id"] = int(delivery_id)
    save_manifest(manifest)


def relock_manifest() -> None:
    manifest = load_manifest()
    if not manifest:
        return
    manifest["armed"] = False
    manifest["relocked_at_utc"] = _utc_now().isoformat()
    save_manifest(manifest)
