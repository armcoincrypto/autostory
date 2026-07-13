"""P6.4 single-account/single-target live canary authorization (manifest-scoped).

Reuses the P5D-style file manifest pattern. Hard limits:
  account=107, target=1, binding=39, max_live_sends=1
Does not authorize live execution unless P6.4_SINGLE_SEND_ENABLED is true
and the operator arms the manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

MANIFEST_PATH = Path("data/audit/p6_4_scope_manifest.json")
MARKER = "__p6_4_certification__"
PILOT_ACCOUNT = 107
TARGET_ID = 1
BINDING_ID = 39
TARGET_TG_ID = 1775722510
DEFAULT_TTL_MINUTES = 15
MAX_LIVE_SENDS = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def message_sha256(body: str) -> str:
    """Canonical UTF-8 content hash (NFC not applied — use exact approved bytes)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
    story_id: Optional[int] = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    operator_approval_id: str = "",
) -> dict[str, Any]:
    now = _utc_now()
    body = str(message_body)
    manifest = {
        "phase": "P6.4",
        "authorization_id": str(uuid.uuid4()),
        "created_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=int(ttl_minutes))).isoformat(),
        "account_id": PILOT_ACCOUNT,
        "binding_id": BINDING_ID,
        "target_id": TARGET_ID,
        "target_tg_id": TARGET_TG_ID,
        "story_id": int(story_id) if story_id is not None else None,
        "message_body": body,
        "expected_message_sha256": message_sha256(body),
        "max_live_sends": MAX_LIVE_SENDS,
        "operator_approved": True,
        "operator_approval_id": operator_approval_id or None,
        "job_id": None,
        "gateway_job_id": None,
        "delivery_id": None,
        "armed": False,
        "consumed": False,
        "reserved": False,
        "scope": "single_account_single_target_live_canary",
        "job_marker": MARKER,
    }
    save_manifest(manifest)
    return manifest


def p6_4_single_send_enabled() -> bool:
    return os.environ.get("P6_4_SINGLE_SEND_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def p6_4_gateway_claim_extra_account_ids() -> frozenset[int]:
    if not p6_4_single_send_enabled():
        return frozenset()
    manifest = load_manifest()
    if manifest and manifest.get("armed") and not manifest.get("consumed"):
        return frozenset({PILOT_ACCOUNT})
    return frozenset()


def validate_p6_4_authorization(
    *,
    account_id: Optional[int] = None,
    target_id: Optional[int] = None,
    binding_id: Optional[int] = None,
    job_marker: Optional[str] = None,
    job_id: Optional[int] = None,
    content_sha256: Optional[str] = None,
    require_armed: bool = False,
    allow_consumed: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    audit: dict[str, Any] = {}
    if str(job_marker or "").strip() != MARKER:
        return False, "p6_4_job_not_fresh", {"job_marker": job_marker}
    manifest = load_manifest()
    if not manifest:
        return False, "p6_4_scope_inactive", audit
    audit["authorization_id"] = manifest.get("authorization_id")
    if manifest.get("invalidated"):
        return False, "p6_4_authorization_invalidated", audit
    if manifest.get("consumed") and not allow_consumed:
        return False, "p6_4_authorization_consumed", audit
    if manifest.get("reserved") and not allow_consumed:
        if job_id is None or int(manifest.get("job_id") or 0) != int(job_id):
            return False, "p6_4_authorization_consumed", audit
    try:
        expires = datetime.fromisoformat(str(manifest["expires_at_utc"]).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if _utc_now() > expires:
            return False, "p6_4_authorization_expired", audit
    except (KeyError, ValueError, TypeError):
        return False, "p6_4_scope_inactive", audit
    if require_armed and not manifest.get("armed"):
        return False, "p6_4_scope_inactive", audit
    if int(manifest.get("account_id") or 0) != PILOT_ACCOUNT:
        return False, "p6_4_account_mismatch", audit
    if account_id is not None and int(account_id) != PILOT_ACCOUNT:
        return False, "p6_4_account_mismatch", audit
    if target_id is not None and int(manifest.get("target_id") or 0) != int(target_id):
        return False, "p6_4_target_mismatch", audit
    if int(manifest.get("binding_id") or 0) != BINDING_ID:
        return False, "p6_4_binding_mismatch", audit
    if binding_id is not None and int(binding_id) != BINDING_ID:
        return False, "p6_4_binding_mismatch", audit
    if job_id is not None and manifest.get("job_id") is not None:
        if int(manifest["job_id"]) != int(job_id):
            return False, "p6_4_job_not_fresh", audit
    expected = str(manifest.get("expected_message_sha256") or "")
    if require_armed and not expected:
        return False, "p6_4_content_hash_missing", audit
    if require_armed and content_sha256 is None:
        return False, "p6_4_content_hash_missing", audit
    if content_sha256 is not None:
        if expected and expected != str(content_sha256).strip().lower():
            return False, "p6_4_content_hash_mismatch", {
                **audit,
                "expected_message_sha256": expected,
                "got_sha256": str(content_sha256).strip().lower(),
            }
    if int(manifest.get("max_live_sends") or 0) != MAX_LIVE_SENDS:
        return False, "p6_4_max_send_misconfigured", audit
    if not p6_4_single_send_enabled() and require_armed:
        return False, "p6_4_scope_inactive", audit
    return True, "p6_4_live_canary_authorized", audit


def reserve_authorization(*, job_id: int) -> tuple[bool, str]:
    manifest = load_manifest()
    if not manifest:
        return False, "p6_4_scope_inactive"
    if manifest.get("consumed"):
        return False, "p6_4_authorization_consumed"
    if manifest.get("reserved") and int(manifest.get("job_id") or 0) != int(job_id):
        return False, "p6_4_authorization_consumed"
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
        raise RuntimeError("No P6.4 manifest to arm")
    manifest["armed"] = bool(armed)
    manifest["armed_at_utc"] = _utc_now().isoformat() if armed else None
    save_manifest(manifest)


def invalidate_unconsumed() -> None:
    """Emergency stop: disarm and invalidate unused authorization."""
    manifest = load_manifest()
    if not manifest:
        return
    manifest["armed"] = False
    if not manifest.get("consumed"):
        manifest["invalidated"] = True
        manifest["invalidated_at_utc"] = _utc_now().isoformat()
    save_manifest(manifest)


def relock_manifest() -> None:
    manifest = load_manifest()
    if not manifest:
        return
    manifest["armed"] = False
    manifest["relocked_at_utc"] = _utc_now().isoformat()
    save_manifest(manifest)
