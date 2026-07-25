#!/usr/bin/env python3
"""Operator-assisted reauth for an *existing* publishing account (string session).

Reuses Telethon QR (or phone+OTP on a TTY) and updates ``accounts.session_string``
in place only after identity match + read-only Story capability.

Safety:
  - One account at a time
  - No Story publish / message send / Dry Run / controlled live
  - Does not create a new Account row
  - Does not print session strings, OTP, or 2FA values
  - Acquires per-account session lock during the operation

Usage:
  cd /opt/autostory-releases/current
  DATABASE_URL=sqlite:////opt/autostory/data/storyfleet.db \\
  PYTHONPATH=. \\
  /opt/autostory/venv/bin/python /path/to/reauth_existing_publishing_account.py \\
      --account-id 13 --method qr --evidence-dir /path/to/evidence
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.stories import CanSendStoryRequest
from telethon.tl.types import InputPeerSelf

# Ensure repo root on path when invoked as absolute script path.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import settings
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS
from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.database import get_db_context, engine
from src.core.models import Account
from src.core.session_lock import acquire_session_lock, inspect_session_lock
from src.stories.fleet_certification import (
    audit_fleet,
    inspect_session,
    mask_phone,
    normalize_classification,
)
from src.stories.daily_story_counter import stories_today_effective

ROLLBACK_ROOT = Path("/opt/autostory/data/session_backups_reauth")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def session_hash(account: Account) -> dict[str, Any]:
    insp = inspect_session(account)
    raw = getattr(account, "session_string", None) or ""
    return {
        "kind": insp.kind,
        "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        if raw
        else None,
        "material_len": len(raw),
        "present": insp.present,
        "readable": insp.readable,
    }


def backup_encrypted_session_blob(account_id: int) -> dict[str, Any]:
    """Copy the on-disk encrypted session column to a restricted rollback file.

    Evidence must never receive session material or QR tokens — only the path + hashes.
    """
    from sqlalchemy import text

    ROLLBACK_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        ROLLBACK_ROOT.chmod(0o700)
    except OSError:
        pass
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT session_string FROM accounts WHERE id = :id"),
            {"id": int(account_id)},
        ).fetchone()
    blob = (row[0] if row else None) or ""
    if not blob:
        return {"ok": False, "reason": "empty_session_blob", "path": None}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROLLBACK_ROOT / f"account_{int(account_id)}.enc.bak.{stamp}"
    path.write_bytes(blob.encode("utf-8") if isinstance(blob, str) else bytes(blob))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "ok": True,
        "path": str(path),
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def assert_publishing_intent(account: Account) -> None:
    aid = int(account.id)
    if aid in CONTROLLER_ACCOUNT_IDS or aid in PROTECTED_IDS:
        raise SystemExit(f"CONFIG_REVIEW_REQUIRED: account {aid} is protected/controller")
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        raise SystemExit(f"CONFIG_REVIEW_REQUIRED: account {aid} is AI-reserved")
    if aid in PURPOSE_HOLD_IDS or (account.purpose or "").lower() == "disabled":
        raise SystemExit(f"CONFIG_REVIEW_REQUIRED: account {aid} is intentionally disabled/hold")
    status = str(getattr(account.status, "value", account.status) or "").lower()
    if status != "active":
        raise SystemExit(f"CONFIG_REVIEW_REQUIRED: account {aid} status={status}")
    if account.user_id is None or not str(account.phone_number or "").strip():
        raise SystemExit(f"CONFIG_REVIEW_REQUIRED: account {aid} identity incomplete")


async def read_only_story_ok(client: TelegramClient) -> tuple[bool, str]:
    try:
        await client(CanSendStoryRequest(peer=InputPeerSelf()))
        return True, "allowed"
    except FloodWaitError as exc:
        return False, f"flood_wait:{exc.seconds}"
    except Exception as exc:
        name = type(exc).__name__
        # Same-day limit is capacity, not capability failure for readiness.
        if name in {"PremiumAccountRequiredError", "BoostsRequiredError", "StoriesTooMuchError"}:
            return True, f"capacity_{name}"
        return False, f"{name}"


async def persist_session_string(account_id: int, session_string: str) -> None:
    with get_db_context() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise RuntimeError(f"account {account_id} vanished")
        account.session_string = session_string
        account.last_active = datetime.utcnow()
        db.commit()


async def verify_reopen(account_id: int, expected_user_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        account = db.get(Account, account_id)
        assert account is not None
        session_string = account.session_string
    client = TelegramClient(
        StringSession(session_string),
        settings.telegram.api_id,
        settings.telegram.api_hash,
        receive_updates=False,
    )
    try:
        await client.connect()
        authorized = await client.is_user_authorized()
        if not authorized:
            return {"ok": False, "reason": "unauthorized_after_write"}
        me = await client.get_me()
        if int(me.id) != int(expected_user_id):
            return {
                "ok": False,
                "reason": "identity_mismatch_after_write",
                "telegram_user_id": int(me.id),
            }
        story_ok, story_status = await read_only_story_ok(client)
        return {
            "ok": bool(story_ok),
            "authorized": True,
            "telegram_user_id": int(me.id),
            "username": me.username,
            "story_status": story_status,
        }
    finally:
        await client.disconnect()


async def reauth_qr(account: Account, evidence_dir: Path, timeout: float) -> dict[str, Any]:
    client = TelegramClient(
        StringSession(),
        settings.telegram.api_id,
        settings.telegram.api_hash,
        receive_updates=False,
    )
    result: dict[str, Any] = {"method": "qr", "account_id": int(account.id)}
    try:
        await client.connect()
        qr = await client.qr_login()
        url = qr.url
        # QR tokens must never be written to evidence/logs on disk — stdout only for the operator TTY.
        result["qr_url_present"] = bool(url)
        print(f"QR_LOGIN_URL={url}", flush=True)
        print(
            f"Scan this QR with the Telegram account that must be user_id={account.user_id} "
            f"(@{account.username or 'n/a'}). Waiting up to {int(timeout)}s…",
            flush=True,
        )
        try:
            user = await qr.wait(timeout=timeout)
        except Exception as exc:
            result.update({"success": False, "error": f"qr_wait:{type(exc).__name__}"})
            return result
        me = user or await client.get_me()
        expected = int(account.user_id)
        got = int(me.id)
        result["telegram_user_id"] = got
        result["telegram_username"] = me.username
        if got != expected:
            result.update(
                {
                    "success": False,
                    "error": "IDENTITY_MISMATCH",
                    "expected_user_id": expected,
                    "got_user_id": got,
                }
            )
            return result
        story_ok, story_status = await read_only_story_ok(client)
        result["story_status"] = story_status
        if not story_ok:
            result.update({"success": False, "error": f"STORY_CAPABILITY_FAILED:{story_status}"})
            return result
        result["session_rollback"] = backup_encrypted_session_blob(int(account.id))
        new_session = client.session.save()
        await persist_session_string(int(account.id), new_session)
        reopen = await verify_reopen(int(account.id), expected)
        result["reopen"] = reopen
        result["success"] = bool(reopen.get("ok"))
        result["2fa_required"] = False
        if not result["success"]:
            result["error"] = reopen.get("reason", "reopen_failed")
        return result
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def reauth_phone(account: Account, evidence_dir: Path) -> dict[str, Any]:
    del evidence_dir  # reserved for parity; secrets must never be written here
    if not sys.stdin.isatty():
        return {
            "success": False,
            "method": "phone",
            "error": "NO_TTY",
            "message": "Phone+OTP requires a real TTY for hidden OTP/2FA input.",
        }
    phone = str(account.phone_number).strip()
    client = TelegramClient(
        StringSession(),
        settings.telegram.api_id,
        settings.telegram.api_hash,
        receive_updates=False,
    )
    result: dict[str, Any] = {"method": "phone", "account_id": int(account.id)}
    try:
        await client.connect()
        await client.send_code_request(phone)
        code = getpass.getpass(f"OTP for account {account.id} (input hidden): ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
            result["2fa_required"] = False
        except SessionPasswordNeededError:
            result["2fa_required"] = True
            password = getpass.getpass("2FA password (input hidden): ")
            await client.sign_in(password=password)
        except PhoneCodeInvalidError:
            return {"success": False, "method": "phone", "error": "invalid_otp"}
        # Drop secrets from locals ASAP
        code = ""
        password = ""  # noqa: F841
        me = await client.get_me()
        expected = int(account.user_id)
        got = int(me.id)
        result["telegram_user_id"] = got
        result["telegram_username"] = me.username
        if got != expected:
            return {
                "success": False,
                "method": "phone",
                "error": "IDENTITY_MISMATCH",
                "expected_user_id": expected,
                "got_user_id": got,
                "2fa_required": result.get("2fa_required"),
            }
        story_ok, story_status = await read_only_story_ok(client)
        result["story_status"] = story_status
        if not story_ok:
            return {
                "success": False,
                "method": "phone",
                "error": f"STORY_CAPABILITY_FAILED:{story_status}",
                "2fa_required": result.get("2fa_required"),
            }
        result["session_rollback"] = backup_encrypted_session_blob(int(account.id))
        await persist_session_string(int(account.id), client.session.save())
        reopen = await verify_reopen(int(account.id), expected)
        result["reopen"] = reopen
        result["success"] = bool(reopen.get("ok"))
        if not result["success"]:
            result["error"] = reopen.get("reason", "reopen_failed")
        return result
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def isolated_audit(account_id: int) -> dict[str, Any]:
    report = await audit_fleet(timeout_seconds=30.0, account_ids={account_id})
    row = report["accounts"][0]
    return {
        "audit_run_id": report["audit_run_id"],
        "classification": normalize_classification(row["classification"]),
        "reason_codes": row.get("reason_codes"),
        "auth_valid": row.get("auth_valid"),
        "identity_matches": row.get("identity_matches"),
        "story_api_available": row.get("story_api_available"),
        "story_probe_status": row.get("story_probe_status"),
        "production_mutations": report.get("production_mutations"),
        "row": {
            k: row[k]
            for k in row
            if k
            not in {
                "session_diagnostics",
                "certification_evidence",
            }
        },
    }


async def main_async(args: argparse.Namespace) -> int:
    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    aid = int(args.account_id)

    lock_state = inspect_session_lock(aid)
    pre = {
        "captured_at": utc_now(),
        "account_id": aid,
        "lock_before": {
            "held": lock_state.held,
            "pids": list(getattr(lock_state, "holder_pids", ()) or ()),
            "subsystem": getattr(lock_state, "subsystem", None),
        },
    }
    with get_db_context() as db:
        account = db.get(Account, aid)
        if account is None:
            raise SystemExit(f"account {aid} not found")
        assert_publishing_intent(account)
        pre.update(
            {
                "status": str(getattr(account.status, "value", account.status)),
                "purpose": account.purpose,
                "telegram_user_id": int(account.user_id),
                "username": account.username,
                "phone_redacted": mask_phone(account.phone_number),
                "session_before": session_hash(account),
                "stories_today_effective": stories_today_effective(account),
            }
        )
    (evidence_dir / f"account-{aid}-preauth.md").write_text(
        "# Account {aid} preauth\n\n```json\n{body}\n```\n".format(
            aid=aid, body=json.dumps(pre, indent=2)
        ),
        encoding="utf-8",
    )

    ok, handle, err = acquire_session_lock(
        aid,
        timeout_sec=20.0,
        subsystem="operator_reauth",
        operation=f"reauth_{args.method}",
    )
    if not ok:
        print(json.dumps({"success": False, "error": f"lock_failed:{err}"}))
        return 2

    auth_result: dict[str, Any]
    try:
        # Reload account inside lock
        with get_db_context() as db:
            account = db.get(Account, aid)
            assert account is not None
        if args.method == "qr":
            auth_result = await reauth_qr(account, evidence_dir, timeout=float(args.qr_timeout))
        else:
            auth_result = await reauth_phone(account, evidence_dir)
    finally:
        handle.release()

    with get_db_context() as db:
        account = db.get(Account, aid)
        assert account is not None
        after_hash = session_hash(account)

    auth_result["session_after"] = after_hash
    auth_result["session_changed"] = after_hash.get("sha256") != pre["session_before"].get(
        "sha256"
    )
    # Never persist secrets / QR tokens in evidence artifacts
    for key in ("session_string", "password", "code", "otp", "qr_url", "url"):
        auth_result.pop(key, None)

    (evidence_dir / f"account-{aid}-auth-result.md").write_text(
        "# Account {aid} auth result\n\n```json\n{body}\n```\n".format(
            aid=aid, body=json.dumps(auth_result, indent=2, default=str)
        ),
        encoding="utf-8",
    )

    if not auth_result.get("success"):
        print(json.dumps({"success": False, "auth": auth_result}, default=str))
        return 1

    audit = await isolated_audit(aid)
    (evidence_dir / f"account-{aid}-isolated-audit.json").write_text(
        json.dumps(audit, indent=2, default=str) + "\n", encoding="utf-8"
    )
    rollback = auth_result.get("session_rollback") or {}
    (evidence_dir / f"account-{aid}-session-verification.md").write_text(
        (
            f"# Account {aid} session verification\n\n"
            f"- before_sha256: `{pre['session_before'].get('sha256')}`\n"
            f"- after_sha256: `{after_hash.get('sha256')}`\n"
            f"- session_changed: `{auth_result.get('session_changed')}`\n"
            f"- reopen_ok: `{auth_result.get('reopen', {}).get('ok')}`\n"
            f"- isolated_classification: `{audit.get('classification')}`\n"
            f"- rollback_path: `{rollback.get('path')}`\n"
            f"- rollback_blob_sha256: `{rollback.get('sha256')}`\n"
            "- rollback_restore: copy encrypted blob from rollback_path back into "
            "`accounts.session_string` for this account id only "
            "(material not stored in evidence)\n"
        ),
        encoding="utf-8",
    )

    ready = audit.get("classification") == "READY_FOR_SEPARATE_CONTROLLED_CANARY"
    print(
        json.dumps(
            {
                "success": ready,
                "auth_success": True,
                "classification": audit.get("classification"),
                "session_changed": auth_result.get("session_changed"),
                "2fa_required": auth_result.get("2fa_required"),
            },
            default=str,
        )
    )
    return 0 if ready else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reauth existing publishing account")
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--method", choices=("qr", "phone"), default="qr")
    p.add_argument("--evidence-dir", required=True)
    p.add_argument("--qr-timeout", type=float, default=120.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    # Fail closed if execution flags somehow enabled
    from src.stories.fleet_certification import assert_read_only_safety

    assert_read_only_safety()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
