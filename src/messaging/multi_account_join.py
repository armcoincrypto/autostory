"""Multi-account join orchestration for Messages (Wave R).

Delegates each join to OwnerChatService.join_async → join_ref_for_account.
Bounded sequential execution. Never sends or schedules.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.models import Account
from src.messaging.eligibility import evaluate_dm_account_eligibility
from src.messaging.multi_account_schedule import (
    MATRIX_ACCOUNT_CAP,
    map_readiness_status,
)
from src.messaging.owner_chat_service import OwnerChatService

JOIN_BATCH_CAP = 10
JOIN_PAUSE_SEC = 1.25
# Known statuses where another join request must not be sent.
# temp_unavailable is NOT included — preview failure falls through to one join attempt.
SKIP_JOIN_STATUSES = frozenset(
    {
        "ready",
        "waiting_approval",
        "cannot_post",
        "protected",
        "reserved",
        "disabled",
        "needs_login",
        "unavailable",
        "already_joined",
    }
)


@dataclass
class BulkJoinResult:
    ok: bool
    status_code: int
    payload: dict[str, Any]


def _account_display(a: Account) -> str:
    return (
        (a.first_name and str(a.first_name).strip())
        or (a.username and f"@{a.username}")
        or f"Account #{a.id}"
    )


def map_join_owner_status(result: dict[str, Any]) -> tuple[str, str]:
    """Map join result to owner-safe (status_key, label)."""
    status = str(result.get("status") or "").strip().lower()
    err = str(result.get("error") or result.get("error_code") or "").strip().upper()
    if status in {"joined"}:
        return "joined", "Joined"
    if status in {"already_joined"}:
        return "already_joined", "Already joined"
    if status in {"join_requested"}:
        return "waiting_approval", "Waiting for approval"
    if status in {"no_permission_to_post"}:
        return "cannot_post", "Cannot post"
    if err in {"PROTECTED", "ACCOUNT_PROTECTED"}:
        return "protected", "Protected"
    if err in {"RESERVED", "ACCOUNT_RESERVED"}:
        return "reserved", "Reserved"
    if err in {"DISABLED", "ACCOUNT_DISABLED"}:
        return "disabled", "Disabled"
    if err in {"AUTH_FAILED", "AUTH_REQUIRED", "NEEDS_SESSION"}:
        return "needs_login", "Needs login"
    if err in {"FLOOD_WAIT", "FLOODWAIT", "PEER_FLOOD", "PEERFLOOD"} or "FLOOD" in err:
        return "failed", "Temporarily unavailable"
    if status in {"denied"}:
        return "failed", "Cannot join"
    if not result.get("ok"):
        return "failed", "Failed"
    return "failed", "Failed"


class MultiAccountJoinOrchestrator:
    """Thin sequential join batch over OwnerChatService."""

    def __init__(
        self,
        *,
        chat_service: Optional[OwnerChatService] = None,
        run_async=None,
        pause_sec: float = JOIN_PAUSE_SEC,
    ) -> None:
        self._chat = chat_service or OwnerChatService()
        self._pause_sec = float(pause_sec)
        if run_async is None:
            from src.clients.telethon_runtime import run as telethon_run

            self._run_async = telethon_run
        else:
            self._run_async = run_async

    def join_bulk(
        self,
        db: Session,
        *,
        ref: str,
        account_ids: list[int],
        confirm: bool = False,
    ) -> BulkJoinResult:
        raw = (ref or "").strip()
        if not raw:
            return BulkJoinResult(
                False,
                400,
                {"ok": False, "error": "PEER_INVALID", "message": "Chat reference required."},
            )
        if not confirm:
            return BulkJoinResult(
                False,
                400,
                {
                    "ok": False,
                    "error": "CONFIRM_REQUIRED",
                    "message": "Confirm is required to request joins.",
                },
            )

        ids: list[int] = []
        for x in account_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        ids = ids[:JOIN_BATCH_CAP]
        if not ids:
            return BulkJoinResult(
                False,
                400,
                {"ok": False, "error": "VALIDATION_ERROR", "message": "account_ids required."},
            )

        accounts = {
            int(a.id): a
            for a in db.query(Account).filter(Account.id.in_(ids)).all()
        }
        results: list[dict[str, Any]] = []
        counts = {
            "joined": 0,
            "waiting_approval": 0,
            "already_joined": 0,
            "failed": 0,
            "skipped": 0,
        }

        for i, aid in enumerate(ids):
            acc = accounts.get(aid)
            display = _account_display(acc) if acc else f"Account #{aid}"
            elig = evaluate_dm_account_eligibility(db, int(aid))
            if not elig.eligible:
                key, label, _ = map_readiness_status(
                    eligible=False,
                    eligibility_code=elig.code,
                    preview=None,
                )
                results.append(
                    {
                        "account_id": aid,
                        "display_name": display,
                        "status": key,
                        "status_label": label if label != "Temporarily unavailable" else "Cannot join",
                        "ok": False,
                        "skipped": True,
                        "message": elig.reason or label,
                    }
                )
                counts["skipped"] += 1
                continue

            # Preview first when available — never re-request while Waiting / Ready.
            preview = None
            preview_ok = False
            if hasattr(self._chat, "preview_async"):
                try:
                    preview = self._run_async(self._chat.preview_async(int(aid), raw))
                    preview_ok = bool(isinstance(preview, dict) and preview.get("ok"))
                except Exception:
                    preview = None
                    preview_ok = False
            if preview_ok:
                status_key, status_label, ready = map_readiness_status(
                    eligible=True,
                    eligibility_code=elig.code,
                    preview=preview if isinstance(preview, dict) else None,
                )
                if ready or status_key in SKIP_JOIN_STATUSES:
                    if ready:
                        status_key, status_label = "already_joined", "Already joined"
                    results.append(
                        {
                            "account_id": aid,
                            "display_name": display,
                            "status": status_key,
                            "status_label": status_label,
                            "ok": True
                            if ready or status_key == "waiting_approval"
                            else False,
                            "skipped": True,
                            "message": (
                                "Already waiting for admin approval — not requesting again."
                                if status_key == "waiting_approval"
                                else status_label
                            ),
                        }
                    )
                    counts["skipped"] += 1
                    if status_key == "waiting_approval":
                        counts["waiting_approval"] += 1
                    elif status_key == "already_joined":
                        counts["already_joined"] += 1
                    continue

            try:
                outcome = self._run_async(
                    self._chat.join_async(int(aid), raw, confirm=True)
                )
            except Exception as e:
                outcome = {
                    "ok": False,
                    "status": "failed",
                    "error": "TEMP_ERROR",
                    "message": str(e)[:200] or "Join failed",
                }

            status_key, status_label = map_join_owner_status(outcome or {})
            row = {
                "account_id": aid,
                "display_name": display,
                "status": status_key,
                "status_label": status_label,
                "ok": bool((outcome or {}).get("ok")),
                "skipped": False,
                "message": (outcome or {}).get("message") or status_label,
                "peer_id": (outcome or {}).get("peer_id"),
                "title": (outcome or {}).get("title"),
                "chat_type": (outcome or {}).get("chat_type"),
                "can_post": (outcome or {}).get("can_post"),
            }
            results.append(row)
            if status_key in counts:
                counts[status_key] += 1
            elif status_key == "cannot_post":
                counts["failed"] += 1
            else:
                counts["failed"] += 1

            # Bounded pacing — stop further joins on flood signal.
            err = str((outcome or {}).get("error") or (outcome or {}).get("error_code") or "").upper()
            if "FLOOD" in err or "PEER_FLOOD" in err:
                for remaining in ids[i + 1 :]:
                    a2 = accounts.get(remaining)
                    results.append(
                        {
                            "account_id": remaining,
                            "display_name": _account_display(a2) if a2 else f"Account #{remaining}",
                            "status": "skipped",
                            "status_label": "Skipped",
                            "ok": False,
                            "skipped": True,
                            "message": "Stopped after rate limit — try again later.",
                        }
                    )
                    counts["skipped"] += 1
                break

            if i + 1 < len(ids) and self._pause_sec > 0:
                time.sleep(self._pause_sec)

        requested = len(ids)
        return BulkJoinResult(
            True,
            200,
            {
                "ok": True,
                "ref": raw,
                "requested": requested,
                "joined": counts["joined"],
                "waiting_approval": counts["waiting_approval"],
                "already_joined": counts["already_joined"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
                "results": results,
                "transport_send_count": 0,
                "scheduled_count": 0,
                "auto_send": False,
                "auto_schedule": False,
                "message": (
                    f"Requested: {requested}. Joined: {counts['joined']}. "
                    f"Waiting approval: {counts['waiting_approval']}. "
                    f"Already joined: {counts['already_joined']}. "
                    f"Failed: {counts['failed']}. Skipped: {counts['skipped']}."
                ),
            },
        )
