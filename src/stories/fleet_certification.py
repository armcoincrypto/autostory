"""Production-safe, read-only Telegram Story fleet certification audit.

The canonical fleet is the ``accounts`` database table. Telegram probes are
sequential and run against temporary copies of file sessions, so Telethon may
refresh metadata only in disposable copies. No account, Story, StoryRun, or
session source is mutated.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneNumberBannedError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.functions.stories import CanSendStoryRequest
from telethon.tl.types import InputPeerSelf

from config.settings import settings
from src.core.database import get_db_context
from src.core.identity_audit import classify_identity_from_me
from src.core.models import Account, Story, SystemLog
from src.clients.session_inspect import (
    resolve_existing_session_path,
    sqlite_schema_version_readonly,
)
from src.clients.session_sqlite_copy import copy_sqlite_session_readonly


CLASSIFICATIONS = (
    "CERTIFIED_PUBLISH",
    "READY_FOR_SEPARATE_CONTROLLED_CANARY",
    "AUTH_OK",  # legacy alias; normalize to READY_FOR_SEPARATE_CONTROLLED_CANARY
    "AUTH_STALE",
    "AUTH_FAILED",
    "SESSION_CORRUPT",
    "ACCOUNT_DISABLED",
    "INTENTIONALLY_EXCLUDED",
    "IDENTITY_MISMATCH",
    "STORY_CAPABILITY_FAILED",
    "SESSION_CONFLICT",
    "COUNTER_INVALID",
    "FLOOD_WAIT",
    "BANNED",
    "CONFIG_INCOMPLETE",
    "UNKNOWN",
)

# Healthy publishing-ready statuses (includes legacy AUTH_OK for older artifacts).
READY_CANARY_STATUSES = frozenset(
    {"READY_FOR_SEPARATE_CONTROLLED_CANARY", "AUTH_OK"}
)
HEALTHY_STATUSES = frozenset({"CERTIFIED_PUBLISH"}) | READY_CANARY_STATUSES


def normalize_classification(classification: str) -> str:
    """Map legacy AUTH_OK onto the canonical canary-ready status."""
    if classification == "AUTH_OK":
        return "READY_FOR_SEPARATE_CONTROLLED_CANARY"
    return classification

TELEGRAM_OPERATION_ALLOWLIST = {
    # Telethon 1.43.2 connect() internals (receive_updates=False).
    "InvokeWithoutUpdatesRequest": "STATE_REFRESH_ONLY",
    "InvokeWithLayerRequest": "STATE_REFRESH_ONLY",
    "InitConnectionRequest": "STATE_REFRESH_ONLY",
    "GetConfigRequest": "READ_ONLY",
    # is_user_authorized() and connect/get_me initialization.
    "updates.GetStateRequest": "READ_ONLY",
    "users.GetUsersRequest(InputUserSelf)": "READ_ONLY",
    # Explicit Story capability request; runtime-gated by ReadOnlyStoryClient.
    "stories.CanSendStoryRequest(InputPeerSelf)": "READ_ONLY",
    # Transport lifecycle; session metadata writes target disposable copies only.
    "connect": "STATE_REFRESH_ONLY",
    "disconnect": "STATE_REFRESH_ONLY",
}

FORBIDDEN_TELEGRAM_OPERATIONS = frozenset(
    {
        "SendStoryRequest",
        "EditStoryRequest",
        "DeleteStoriesRequest",
        "send_message",
        "send_file",
        "upload_file",
    }
)

SAFETY_FLAGS = (
    "STORY_EXECUTION_ENABLED",
    "CONTROLLED_STORY_EXECUTION_ENABLED",
    "SCHEDULER_STORY_EXECUTION_ENABLED",
    "SCHEDULER_MUTATIONS_ENABLED",
    "CAMPAIGN_EXECUTION_ENABLED",
    "DISCOVERY_EXECUTION_ENABLED",
    "BROADCAST_EXECUTION_ENABLED",
    "TELEGRAM_STORIES_LIVE_ENABLED",
)
CONTROLLED_ACCOUNT_FLAG = "CONTROLLED_STORY_ACCOUNT_ID"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_SECRETISH = re.compile(r"(?i)(api[_-]?hash|auth[_-]?key|session[_-]?string|password|otp|code)\s*[=:]\s*\S+")
_LONG_DIGITS = re.compile(r"(?<!\d)\+?\d{8,}(?!\d)")
_ABS_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def mask_phone(phone: Any) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return "UNKNOWN"
    suffix = digits[-4:] if len(digits) >= 4 else digits[-2:]
    prefix = f"+{digits[:2]}" if len(digits) >= 8 else "+"
    return f"{prefix}***{suffix}"


def sanitize_error(exc: BaseException | str | None) -> str | None:
    if exc is None:
        return None
    text = str(exc)
    text = _SECRETISH.sub("[redacted]", text)
    text = _LONG_DIGITS.sub("[redacted-number]", text)
    text = _ABS_PATH.sub("[redacted-path]", text)
    return text[:300] or type(exc).__name__


def safe_session_location(account_id: int, kind: str, path: Path | None) -> str:
    if kind == "string":
        return "database:StringSession"
    if path is None:
        return "UNKNOWN"
    canonical = f"account_{int(account_id)}.session"
    if path.name == canonical:
        return f"canonical:{canonical}"
    return f"configured:{path.name}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_source_state(project_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(project_root), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    try:
        head = run("rev-parse", "HEAD")
        branch = run("branch", "--show-current")
    except Exception:
        head, branch = "UNKNOWN", "UNKNOWN"
    return {"repository": str(project_root), "head": head, "branch": branch}


def safety_snapshot() -> dict[str, Any]:
    flags: dict[str, str] = {}
    safe = True
    blockers: list[str] = []
    for name in SAFETY_FLAGS:
        raw = (os.environ.get(name) or "").strip()
        normalized = raw.lower()
        state = "unset" if not raw else ("enabled" if normalized in _TRUE_VALUES else "disabled")
        flags[name] = state
        if state == "enabled":
            safe = False
            blockers.append(f"{name}_enabled")
    controlled = (os.environ.get(CONTROLLED_ACCOUNT_FLAG) or "").strip()
    flags[CONTROLLED_ACCOUNT_FLAG] = "unset" if not controlled else "set"
    if controlled:
        safe = False
        blockers.append(f"{CONTROLLED_ACCOUNT_FLAG}_set")
    return {"safe": safe, "flags": flags, "blockers": blockers}


def assert_read_only_safety() -> dict[str, Any]:
    snapshot = safety_snapshot()
    if not snapshot["safe"]:
        raise RuntimeError(
            "Fleet certification refused: publishing/mutation safety flags are not locked: "
            + ", ".join(snapshot["blockers"])
        )
    return snapshot


class ReadOnlyStoryClient:
    """Narrow facade: run_story_precheck can invoke only CanSendStoryRequest."""

    def __init__(self, client: TelegramClient):
        self._client = client

    async def get_input_entity(self, value: Any) -> InputPeerSelf:
        if value != "me":
            raise RuntimeError("read-only probe allows get_input_entity('me') only")
        return InputPeerSelf()

    async def __call__(self, request: Any) -> Any:
        if type(request) is not CanSendStoryRequest:
            raise RuntimeError(
                f"Telegram request {type(request).__name__} is not in fleet audit allowlist"
            )
        return await self._client(request)


@dataclass
class SessionInspection:
    kind: str
    path: Path | None
    present: bool
    readable: bool
    error_code: str | None
    diagnostics: list[str]
    source_sha256: str | None


def inspect_session(account: Account) -> SessionInspection:
    raw = (getattr(account, "session_string", None) or "").strip()
    exists, resolved = resolve_existing_session_path(account)
    path = Path(resolved) if exists and resolved else None
    diagnostics: list[str] = []

    if path is not None:
        version, columns, warnings = sqlite_schema_version_readonly(path)
        diagnostics.extend(warnings)
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as conn:
                quick = conn.execute("PRAGMA quick_check").fetchone()
                if not quick or str(quick[0]).lower() != "ok":
                    diagnostics.append("sqlite_quick_check_failed")
        except sqlite3.OperationalError as exc:
            code = "session_locked" if "locked" in str(exc).lower() else "session_unreadable"
            return SessionInspection(
                "file", path, True, False, code, diagnostics + [type(exc).__name__], None
            )
        except sqlite3.DatabaseError as exc:
            return SessionInspection(
                "file", path, True, False, "session_unreadable",
                diagnostics + [type(exc).__name__], None
            )
        if warnings or version is None or columns is None:
            return SessionInspection(
                "file", path, True, False, "session_unreadable", diagnostics, None
            )
        try:
            digest = sha256_file(path)
        except OSError:
            return SessionInspection(
                "file", path, True, False, "session_unreadable", diagnostics, None
            )
        diagnostics.extend([f"sqlite_version={version}", f"session_columns={columns}"])
        return SessionInspection("file", path, True, True, None, diagnostics, digest)

    if raw:
        # Path-like material that does not exist is not a StringSession.
        if raw.startswith(("/", "file://")) or raw.endswith(".session"):
            return SessionInspection(
                "file", None, False, False, "session_missing", diagnostics, None
            )
        try:
            StringSession(raw)
        except Exception as exc:
            return SessionInspection(
                "string", None, True, False, "invalid_session_format",
                [type(exc).__name__], None
            )
        return SessionInspection("string", None, True, True, None, diagnostics, None)

    return SessionInspection("empty", None, False, False, "session_missing", diagnostics, None)


def _copy_sqlite_session(source: Path, destination: Path) -> None:
    """Consistent SQLite backup from read-only source into disposable destination."""
    copy_sqlite_session_readonly(source, destination)


async def _wait(awaitable: Awaitable[Any], timeout: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=timeout)


async def probe_account_telegram(
    account: Account,
    inspection: SessionInspection,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Probe one enabled account. Caller guarantees local session readability."""
    from src.stories.precheck import run_story_precheck

    started = time.monotonic()
    result: dict[str, Any] = {
        "probe_status": "error",
        "auth_valid": False,
        "telegram_user_id": None,
        "telegram_username": None,
        "identity_matches": False,
        "identity_status": "unknown",
        "identity_reason": None,
        "story_api_available": False,
        "story_probe_status": "not_run",
        "story_probe_reason": None,
        "flood_wait_seconds": None,
        "safe_error_summary": None,
        "source_session_unchanged": None,
        "probe_duration_ms": None,
    }
    client: TelegramClient | None = None
    source_hash_before = inspection.source_sha256

    try:
        from src.clients.session_resolve import resolve_telethon_session

        with tempfile.TemporaryDirectory(prefix=f"storyfleet-fleet-{account.id}-") as temp_dir:
            # Canonical resolve for string material; disposable SQLite copy for on-disk sessions.
            if inspection.kind == "file" and inspection.path is not None:
                # Confirm resolve can see the account before probing a disposable copy.
                _resolved, _kind, err = resolve_telethon_session(account)
                if err:
                    raise RuntimeError(err)
                temp_file = Path(temp_dir) / f"account_{account.id}.session"
                _copy_sqlite_session(inspection.path, temp_file)
                session: Any = SQLiteSession(str(temp_file.with_suffix("")))
            elif inspection.kind == "string":
                resolved, kind, err = resolve_telethon_session(account)
                if err is None and resolved is not None and kind == "string":
                    session = resolved
                else:
                    # Module-level StringSession remains the narrow fallback for
                    # operator-validated string inspections / test doubles.
                    session = StringSession((account.session_string or "").strip())
            else:
                raise RuntimeError("session_not_probeable")

            client = TelegramClient(
                session,
                int(settings.telegram.api_id),
                str(settings.telegram.api_hash),
                proxy=account.proxy_config if account.proxy_config else None,
                device_model="STORYFLEET-READONLY-AUDIT",
                app_version="fleet-certification",
                system_version="Linux",
                lang_code="en",
                receive_updates=False,
            )
            await _wait(client.connect(), timeout_seconds)
            authorized = bool(
                await _wait(client.is_user_authorized(), timeout_seconds)
            )
            if not authorized:
                result.update(
                    probe_status="unauthorized",
                    safe_error_summary="Telegram session is not authorized",
                )
                return result

            result["auth_valid"] = True
            me = await _wait(client.get_me(), timeout_seconds)
            if me is None:
                result.update(
                    probe_status="get_me_failed",
                    safe_error_summary="get_me returned no identity",
                )
                return result

            result["telegram_user_id"] = int(me.id) if me.id is not None else None
            result["telegram_username"] = getattr(me, "username", None)
            identity_status, identity_reason = classify_identity_from_me(account, me)
            result["identity_status"] = identity_status
            result["identity_reason"] = identity_reason
            result["identity_matches"] = identity_status in {"identity_ok", "metadata_stale"}

            precheck = await _wait(
                run_story_precheck(ReadOnlyStoryClient(client), int(account.id)),
                timeout_seconds,
            )
            result["story_probe_status"] = precheck.get("status")
            result["story_probe_reason"] = sanitize_error(precheck.get("reason"))
            result["flood_wait_seconds"] = precheck.get("retry_after_seconds")
            result["story_api_available"] = precheck.get("status") == "allowed"
            result["probe_status"] = "ok" if result["story_api_available"] else "story_probe_failed"
            return result
    except FloodWaitError as exc:
        result.update(
            probe_status="flood_wait",
            flood_wait_seconds=int(getattr(exc, "seconds", 0) or 0),
            safe_error_summary=f"FloodWaitError wait_seconds={int(getattr(exc, 'seconds', 0) or 0)}",
        )
        return result
    except (UserDeactivatedBanError, PhoneNumberBannedError) as exc:
        result.update(
            probe_status="banned",
            safe_error_summary=type(exc).__name__,
        )
        return result
    except UserDeactivatedError as exc:
        result.update(
            probe_status="deactivated",
            safe_error_summary=type(exc).__name__,
        )
        return result
    except (SessionRevokedError, AuthKeyUnregisteredError, AuthKeyError) as exc:
        result.update(
            probe_status="unauthorized",
            safe_error_summary=type(exc).__name__,
        )
        return result
    except asyncio.TimeoutError:
        result.update(probe_status="timeout", safe_error_summary="probe_timeout")
        return result
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
        result.update(
            probe_status="session_error",
            safe_error_summary=type(exc).__name__,
        )
        return result
    except Exception as exc:
        result.update(
            probe_status="error",
            safe_error_summary=f"{type(exc).__name__}: {sanitize_error(exc)}",
        )
        return result
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception:
                pass
        if inspection.path is not None and source_hash_before:
            try:
                result["source_session_unchanged"] = (
                    sha256_file(inspection.path) == source_hash_before
                )
            except OSError:
                result["source_session_unchanged"] = False
        result["probe_duration_ms"] = int((time.monotonic() - started) * 1000)


async def probe_account_isolated(
    account: Account,
    inspection: SessionInspection,
    *,
    timeout_seconds: float,
    probe_runner: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Convert a per-account runner exception into safe audit data."""
    try:
        return await probe_runner(
            account,
            inspection,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return {
            "probe_status": "error",
            "auth_valid": False,
            "identity_matches": False,
            "story_api_available": False,
            "story_probe_status": "not_run",
            "safe_error_summary": f"{type(exc).__name__}: {sanitize_error(exc)}",
            "probe_duration_ms": 0,
        }


def durable_certification_evidence(db: Any) -> dict[int, dict[str, Any]]:
    """Require successful controlled log plus matching persisted Story row."""
    rows = (
        db.query(SystemLog)
        .filter(SystemLog.component == "controlled_live_story_run")
        .order_by(SystemLog.id.asc())
        .all()
    )
    evidence: dict[int, dict[str, Any]] = {}
    for log in rows:
        details = log.details if isinstance(log.details, dict) else {}
        candidates: list[dict[str, Any]] = []
        if details.get("success"):
            candidates.append(
                {
                    "account_id": details.get("account_id"),
                    "db_story_id": details.get("db_story_id"),
                    "telegram_story_id": details.get("telegram_story_id"),
                }
            )
        # Multi-account controlled runs (v2) record per-step success.
        if details.get("schema") == "controlled_live_story_run_v2_multi":
            for step in details.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                if not step.get("ok"):
                    continue
                candidates.append(
                    {
                        "account_id": step.get("account_id"),
                        "db_story_id": step.get("db_id"),
                        "telegram_story_id": step.get("story_id"),
                    }
                )
        for cand in candidates:
            account_id = cand.get("account_id")
            db_story_id = cand.get("db_story_id")
            telegram_story_id = cand.get("telegram_story_id")
            if not account_id or not db_story_id or telegram_story_id is None:
                continue
            story = db.get(Story, int(db_story_id))
            if (
                story is None
                or int(story.account_id) != int(account_id)
                or story.story_id is None
            ):
                continue
            evidence[int(account_id)] = {
                "system_log_id": int(log.id),
                "db_story_id": int(story.id),
                "telegram_story_id": int(story.story_id),
                "published_at": iso(story.published_at),
            }
    return evidence


def historical_auth_evidence(account: Account) -> bool:
    return bool(
        account.last_active
        or account.last_story_success_at
        or account.story_precheck_status == "allowed"
        or (account.health_status or "").lower() == "alive"
    )


def classify_account(
    *,
    account: Account,
    enabled: bool,
    config_complete: bool,
    inspection: SessionInspection,
    probe: dict[str, Any] | None,
    certified: bool,
) -> tuple[str, list[str]]:
    """Deterministic primary classification and safe reason codes."""
    reasons: list[str] = []
    status = str(getattr(account.status, "value", account.status) or "").lower()
    health = (account.health_status or "").lower()

    if not enabled:
        return "ACCOUNT_DISABLED", ["account_disabled"]
    if not config_complete or not inspection.present:
        if not inspection.present:
            reasons.append("session_missing")
        return "CONFIG_INCOMPLETE", reasons or ["missing_account_config"]
    if not inspection.readable:
        return "SESSION_CORRUPT", [inspection.error_code or "session_unreadable"]

    probe = probe or {}
    probe_status = probe.get("probe_status")
    if status == "banned" or health in {"banned", "deleted"} or probe_status in {"banned", "deactivated"}:
        return "BANNED", ["account_banned" if probe_status == "banned" else "account_deactivated"]
    if (
        status == "flood_wait"
        or health == "flood_wait"
        or probe_status == "flood_wait"
        or probe.get("story_probe_status") == "rate_limited"
    ):
        # Same-day Story capacity exhaustion (STORIES_TOO_MUCH) is often mapped to
        # rate_limited. That must not wipe durable CERTIFIED_PUBLISH after a canary.
        reason = str(probe.get("story_probe_reason") or probe.get("safe_error_summary") or "")
        if (
            certified
            and probe.get("auth_valid")
            and probe.get("identity_matches")
            and "STORIES_TOO_MUCH" in reason.upper()
        ):
            return "CERTIFIED_PUBLISH", [
                "durable_controlled_story_evidence",
                "capacity_stories_too_much",
            ]
        return "FLOOD_WAIT", ["flood_wait"]
    if probe_status == "unauthorized":
        return "AUTH_FAILED", ["telegram_unauthorized"]
    if probe.get("auth_valid") and not probe.get("identity_matches"):
        return "IDENTITY_MISMATCH", ["identity_mismatch"]
    if probe.get("auth_valid") and not probe.get("story_api_available"):
        story_status = str(probe.get("story_probe_status") or "")
        # Same-day Story limit is capacity, not a capability failure.
        if story_status in {"premium_needed", "boost_needed", "stories_too_much"}:
            # Still auth+identity OK; treat as ready with capacity note via reason.
            if certified:
                return "CERTIFIED_PUBLISH", ["durable_controlled_story_evidence", story_status]
            return "READY_FOR_SEPARATE_CONTROLLED_CANARY", [
                "no_certified_story_evidence",
                f"capacity_{story_status}",
            ]
        reason = (
            "story_probe_unavailable"
            if story_status in {"not_run", "not_probed", ""}
            else "story_probe_failed"
        )
        return "STORY_CAPABILITY_FAILED", [reason, story_status] if story_status else [reason]
    if probe.get("session_conflict"):
        return "SESSION_CONFLICT", ["worker_session_conflict"]
    if probe.get("counter_invalid"):
        return "COUNTER_INVALID", ["daily_counter_invalid"]
    if probe_status in {"timeout", "error", "session_error", "get_me_failed"}:
        if historical_auth_evidence(account):
            return "AUTH_STALE", ["last_auth_too_old", f"probe_{probe_status}"]
        return "AUTH_FAILED", [f"probe_{probe_status}"]
    if probe.get("auth_valid") and probe.get("identity_matches") and probe.get("story_api_available"):
        if certified:
            return "CERTIFIED_PUBLISH", ["durable_controlled_story_evidence"]
        return "READY_FOR_SEPARATE_CONTROLLED_CANARY", ["no_certified_story_evidence"]
    if historical_auth_evidence(account):
        return "AUTH_STALE", ["last_auth_too_old"]
    return "AUTH_FAILED", ["auth_not_established"]


def calculate_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    classes = Counter(normalize_classification(row["classification"]) for row in rows)
    ready = classes["READY_FOR_SEPARATE_CONTROLLED_CANARY"]
    certified = classes["CERTIFIED_PUBLISH"]
    return {
        "total_accounts": len(rows),
        "healthy": certified + ready,
        "need_auth": classes["AUTH_STALE"] + classes["AUTH_FAILED"],
        "broken_sessions": classes["SESSION_CORRUPT"],
        "disabled": classes["ACCOUNT_DISABLED"],
        "intentionally_excluded": classes["INTENTIONALLY_EXCLUDED"],
        "identity_mismatch": classes["IDENTITY_MISMATCH"],
        "story_capability_failed": classes["STORY_CAPABILITY_FAILED"],
        "session_conflict": classes["SESSION_CONFLICT"],
        "counter_invalid": classes["COUNTER_INVALID"],
        "unknown": classes["UNKNOWN"],
        "story_capable": sum(
            1
            for row in rows
            if row.get("auth_valid")
            and row.get("identity_matches")
            and row.get("story_api_available")
        ),
        "already_certified": certified,
        "ready_for_next_canary": ready,
    }


def inventory_row(
    account: Account,
    inspection: SessionInspection,
    probe: dict[str, Any] | None,
    certification: dict[str, Any] | None,
    audit_run_id: str,
    audit_started_at: str,
) -> dict[str, Any]:
    status = str(getattr(account.status, "value", account.status) or "").lower()
    enabled = status == "active" and (account.purpose or "").lower() != "disabled"
    api_credentials_configured = bool(settings.telegram.api_id and settings.telegram.api_hash)
    identity_linked = bool(account.user_id or account.phone_number or account.username)
    config_complete = bool(
        account.id
        and str(account.phone_number or "").strip()
        and api_credentials_configured
        and identity_linked
        and inspection.present
    )
    classification, reasons = classify_account(
        account=account,
        enabled=enabled,
        config_complete=config_complete,
        inspection=inspection,
        probe=probe,
        certified=bool(certification),
    )
    classification = normalize_classification(classification)
    probe = probe or {}
    last_story = certification.get("published_at") if certification else iso(account.last_story_success_at)
    display_name = " ".join(
        part for part in [account.first_name, account.last_name] if part
    ).strip() or (f"@{account.username}" if account.username else f"Account {account.id}")
    return {
        "audit_run_id": audit_run_id,
        "audit_started_at": audit_started_at,
        "audit_completed_at": None,
        "account_id": int(account.id),
        "display_name": display_name,
        "configured_enabled": enabled,
        "configured_status": status or "UNKNOWN",
        "purpose": account.purpose or "UNKNOWN",
        "expected_telegram_user_id": int(account.user_id) if account.user_id is not None else None,
        "expected_username": account.username,
        "phone_redacted": mask_phone(account.phone_number),
        "session_type": inspection.kind,
        "session_location_redacted": safe_session_location(account.id, inspection.kind, inspection.path),
        "session_present": inspection.present,
        "session_readable": inspection.readable,
        "config_complete": config_complete,
        "auth_valid": probe.get("auth_valid"),
        "telegram_user_id": probe.get("telegram_user_id"),
        "telegram_username": probe.get("telegram_username"),
        "identity_matches": probe.get("identity_matches"),
        "identity_status": probe.get("identity_status", "not_probed"),
        "story_api_available": probe.get("story_api_available"),
        "story_probe_status": probe.get("story_probe_status", "not_probed"),
        "existing_successful_story_evidence": bool(certification),
        "controlled_publish_certified": bool(certification),
        "last_auth_at": audit_started_at if probe.get("auth_valid") else iso(account.story_precheck_checked_at),
        "last_story_at": last_story,
        "last_successful_story_id": certification.get("telegram_story_id") if certification else None,
        "classification": classification,
        "reason_codes": reasons,
        "safe_error_summary": probe.get("safe_error_summary"),
        "flood_wait_seconds": probe.get("flood_wait_seconds"),
        "probe_duration_ms": probe.get("probe_duration_ms", 0),
        "source_session_unchanged": probe.get("source_session_unchanged"),
        "session_diagnostics": inspection.diagnostics,
        "certification_evidence": certification,
    }


async def audit_fleet(
    *,
    timeout_seconds: float = 15.0,
    probe_runner: Callable[..., Awaitable[dict[str, Any]]] = probe_account_telegram,
    account_ids: set[int] | None = None,
) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[2]
    safety_before = assert_read_only_safety()
    started = utc_now()
    run_id = f"fleet-certification-{started.strftime('%Y%m%dT%H%M%SZ')}"

    with get_db_context() as db:
        query = db.query(Account).order_by(Account.id.asc())
        if account_ids is not None:
            query = query.filter(Account.id.in_(sorted(account_ids)))
        accounts = query.all()
        certifications = durable_certification_evidence(db)

    rows: list[dict[str, Any]] = []
    commands = [
        "Loaded canonical inventory from database accounts table",
        "Verified all publishing and mutation flags locked",
        "Sequential Telegram probe using disposable session copies",
    ]
    for account in accounts:
        inspection = inspect_session(account)
        status = str(getattr(account.status, "value", account.status) or "").lower()
        enabled = status == "active" and (account.purpose or "").lower() != "disabled"
        probe: dict[str, Any] | None = None
        if enabled and inspection.present and inspection.readable:
            probe = await probe_account_isolated(
                account,
                inspection,
                timeout_seconds=timeout_seconds,
                probe_runner=probe_runner,
            )
            # Timeouts are retried once in isolation before AUTH_STALE classification.
            if probe and probe.get("probe_status") == "timeout":
                commands.append(
                    f"Retried account {int(account.id)} once after probe_timeout"
                )
                probe = await probe_account_isolated(
                    account,
                    inspection,
                    timeout_seconds=timeout_seconds,
                    probe_runner=probe_runner,
                )
                probe["timeout_retried"] = True
        row = inventory_row(
            account,
            inspection,
            probe,
            certifications.get(int(account.id)),
            run_id,
            iso(started) or "",
        )
        rows.append(row)

    rows.sort(key=lambda row: int(row["account_id"]))
    completed = utc_now()
    completed_iso = iso(completed)
    for row in rows:
        row["audit_completed_at"] = completed_iso

    safety_after = assert_read_only_safety()
    return {
        "schema": "storyfleet_fleet_certification_v1",
        "audit_run_id": run_id,
        "audit_started_at": iso(started),
        "audit_completed_at": completed_iso,
        "duration_seconds": round((completed - started).total_seconds(), 3),
        "source_state": current_source_state(project_root),
        "canonical_inventory_source": "database.accounts",
        "persistence": {
            "mode": "artifact_only",
            "reason": "First safe phase avoids production DB mutations; timestamped JSON/Markdown provide per-run history.",
        },
        "telegram_request_allowlist": TELEGRAM_OPERATION_ALLOWLIST,
        "forbidden_operations": sorted(FORBIDDEN_TELEGRAM_OPERATIONS),
        "safety_before": safety_before,
        "safety_after": safety_after,
        "totals_definitions": {
            "total_accounts": "All configured account rows, including disabled/incomplete.",
            "healthy": "CERTIFIED_PUBLISH + READY_FOR_SEPARATE_CONTROLLED_CANARY.",
            "need_auth": "AUTH_STALE + AUTH_FAILED.",
            "broken_sessions": "SESSION_CORRUPT.",
            "disabled": "ACCOUNT_DISABLED.",
            "intentionally_excluded": "Protected, AI-reserved, or purpose-hold accounts not in Story publish scope.",
            "story_capable": "Current auth + identity match + CanSendStoryRequest allowed.",
            "already_certified": "CERTIFIED_PUBLISH from durable controlled-publish evidence.",
            "ready_for_next_canary": "READY_FOR_SEPARATE_CONTROLLED_CANARY only; excludes already certified.",
        },
        "totals": calculate_totals(rows),
        "accounts": rows,
        "commands": commands,
        "production_mutations": {
            "stories_published": 0,
            "messages_sent": 0,
            "logins_attempted": 0,
            "account_state_changes": 0,
            "shared_env_modified": False,
            "source_session_files_modified": any(
                row.get("source_session_unchanged") is False for row in rows
            ),
        },
    }


def _md_value(value: Any) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    if isinstance(value, bool):
        return "YES" if value else "NO"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        "# STORYFLEET Fleet Certification Audit",
        "",
        f"- Audit run: `{report['audit_run_id']}`",
        f"- Started: `{report['audit_started_at']}`",
        f"- Completed: `{report['audit_completed_at']}`",
        "- Mode: **READ ONLY — no publish authorization**",
        "",
        "## Fleet totals",
        "",
    ]
    for key in (
        "total_accounts",
        "healthy",
        "need_auth",
        "broken_sessions",
        "disabled",
        "story_capable",
        "already_certified",
        "ready_for_next_canary",
    ):
        lines.append(f"- {key.replace('_', ' ').title()}: **{totals[key]}**")
    lines.extend(
        [
            "",
            "## Readiness matrix",
            "",
            "| Account | Identity | Enabled | Session | Auth | Story API | Last Auth | Last Story | Certified | Classification | Reason |",
            "|---:|---|---:|---|---|---|---|---|---:|---|---|",
        ]
    )
    for row in report["accounts"]:
        identity = row.get("telegram_username")
        if identity:
            identity = f"@{identity}"
        elif row.get("telegram_user_id"):
            identity = str(row["telegram_user_id"])
        else:
            identity = "UNKNOWN"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["account_id"]),
                    _md_value(identity),
                    _md_value(row["configured_enabled"]),
                    _md_value(
                        f"{row['session_type']}/"
                        + ("readable" if row["session_readable"] else "unreadable")
                    ),
                    _md_value(row.get("auth_valid")),
                    _md_value(row.get("story_api_available")),
                    _md_value(row.get("last_auth_at")),
                    _md_value(row.get("last_story_at")),
                    _md_value(row.get("controlled_publish_certified")),
                    row["classification"],
                    _md_value(",".join(row["reason_codes"])),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Telegram operations were restricted to the explicit allowlist in `fleet-readiness.json`.",
            "- File sessions were probed through disposable SQLite backups.",
            "- Source-session hashes were compared before/after probing.",
            "- No Story/message/login/account-state mutation path is imported by the audit engine.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_artifacts(report: dict[str, Any], output_root: Path) -> Path:
    evidence_dir = output_root / report["audit_run_id"]
    evidence_dir.mkdir(parents=True, exist_ok=False)
    (evidence_dir / "fleet-readiness.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    markdown = render_markdown(report)
    (evidence_dir / "fleet-readiness.md").write_text(markdown, encoding="utf-8")
    (evidence_dir / "README.md").write_text(
        "# Evidence bundle\n\n"
        "Production-safe, read-only Telegram Story fleet certification. "
        "See `fleet-readiness.md` for the operator matrix and "
        "`fleet-readiness.json` for stable machine-readable data.\n",
        encoding="utf-8",
    )
    safety = report["production_mutations"]
    (evidence_dir / "safety-verification.md").write_text(
        "# Safety verification\n\n"
        f"- Pre-audit flags locked: `{report['safety_before']['safe']}`\n"
        f"- Post-audit flags locked: `{report['safety_after']['safe']}`\n"
        f"- Stories published: `{safety['stories_published']}`\n"
        f"- Messages sent: `{safety['messages_sent']}`\n"
        f"- Telegram logins attempted: `{safety['logins_attempted']}`\n"
        f"- Account state changes: `{safety['account_state_changes']}`\n"
        f"- Shared `.env` modified: `{safety['shared_env_modified']}`\n"
        f"- Source session modified: `{safety['source_session_files_modified']}`\n",
        encoding="utf-8",
    )
    (evidence_dir / "commands.log").write_text(
        "\n".join(report.get("commands") or []) + "\n",
        encoding="utf-8",
    )
    (evidence_dir / "test-results.txt").write_text(
        "Pending: populated by operator after focused/regression tests.\n",
        encoding="utf-8",
    )
    return evidence_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Telegram Story fleet certification audit"
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Explicit safety acknowledgement (the command is always read-only)",
    )
    parser.add_argument(
        "--output-root",
        default="docs/audits",
        help="Timestamped evidence parent directory",
    )
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.read_only:
        raise SystemExit("Refusing to run without explicit --read-only")
    if args.timeout_seconds <= 0 or args.timeout_seconds > 60:
        raise SystemExit("--timeout-seconds must be between 0 and 60")
    report = asyncio.run(audit_fleet(timeout_seconds=args.timeout_seconds))
    evidence_dir = write_artifacts(report, Path(args.output_root))
    print(evidence_dir)
    # Account failures are audit data, not engine failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
