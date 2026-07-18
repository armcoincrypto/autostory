"""P4B — Controlled one-account live story run (account #140 only)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

import structlog
from flask import jsonify

from src.core.database import get_db_context
from src.core.models import StoryRun, StoryRunStep, SystemLog
from src.stories.rotation_audit import (
    CONTROLLED_LIVE_ACCOUNT_ID,
    build_story_rotation_precheck,
)
from src.stories.scheduler_integration import story_execution_enabled

logger = structlog.get_logger(__name__)

LIVE_CONFIRMATION_TOKEN = f"LIVE_STORY_ACCOUNT_{CONTROLLED_LIVE_ACCOUNT_ID}"


def _requested_account_ids(payload: dict[str, Any]) -> list[int] | None:
    raw = payload.get("account_ids") or payload.get("account_id")
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, list):
        return [int(x) for x in raw if x is not None]
    return [int(raw)]


def _error_response(
    error: str,
    status: int,
    *,
    message: str | None = None,
    live_gate_blockers: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    body: dict[str, Any] = {
        "ok": False,
        "error": error,
        "controlled_live_account_id": CONTROLLED_LIVE_ACCOUNT_ID,
    }
    if message:
        body["message"] = message
    if live_gate_blockers:
        body["live_gate_blockers"] = live_gate_blockers
    if extra:
        body.update(extra)
    return body, status


def evaluate_controlled_live_run_gates(
    payload: dict[str, Any],
    report: dict[str, Any],
) -> tuple[dict[str, Any] | None, int | None]:
    """Return ``(error_body, status)`` when blocked, else ``(None, None)``."""
    if not story_execution_enabled():
        return _error_response(
            "story_execution_disabled",
            403,
            message="Story execution is disabled. Set STORY_EXECUTION_ENABLED=true for controlled live only.",
        )

    env_account = os.getenv("CONTROLLED_STORY_ACCOUNT_ID", "").strip()
    if env_account != str(CONTROLLED_LIVE_ACCOUNT_ID):
        return _error_response(
            "controlled_account_flag_required",
            403,
            message=f"Set CONTROLLED_STORY_ACCOUNT_ID={CONTROLLED_LIVE_ACCOUNT_ID} for this path.",
        )

    if payload.get("dry_run"):
        return _error_response(
            "story_precheck_failed",
            409,
            message="Live runs reject dry_run=true. Use POST /api/stories/dry-run instead.",
        )

    requested = _requested_account_ids(payload)
    if requested != [CONTROLLED_LIVE_ACCOUNT_ID]:
        return _error_response(
            "controlled_account_required",
            403,
            message=f"Controlled live runs require account_ids=[{CONTROLLED_LIVE_ACCOUNT_ID}] only.",
            live_gate_blockers=["live_gate_requires_account_140_only"],
        )

    if payload.get("explicit_operator_approval") is not True:
        return _error_response(
            "operator_approval_required",
            403,
            message="Set explicit_operator_approval=true after operator sign-off.",
            live_gate_blockers=["explicit_operator_approval_required"],
        )

    confirmation = str(payload.get("confirmation_token") or "").strip()
    if confirmation != LIVE_CONFIRMATION_TOKEN:
        return _error_response(
            "confirmation_token_required",
            403,
            message=f"confirmation_token must be {LIVE_CONFIRMATION_TOKEN}.",
            live_gate_blockers=["confirmation_token_required"],
        )

    media = report.get("media") or {}
    if not media.get("ok"):
        return _error_response(
            "missing_or_invalid_media",
            400,
            message=media.get("message") or "Media path is missing or invalid.",
            extra={"media": media},
        )

    account_rows = report.get("accounts") or []
    account_row = next(
        (row for row in account_rows if int(row.get("account_id") or 0) == CONTROLLED_LIVE_ACCOUNT_ID),
        None,
    )
    if account_row and account_row.get("blockers"):
        return _error_response(
            "story_precheck_failed",
            409,
            message="Account precheck blockers remain.",
            extra={
                "blockers": account_row.get("blockers"),
                "live_gate_blockers": report.get("live_gate_blockers", []),
            },
        )

    live_only = list(report.get("live_only_blockers") or [])
    fresh_auth_blockers = [
        b
        for b in live_only
        if str(b).startswith("fresh_story_auth") or b in {"auth_required", "fresh_story_auth_required"}
    ]
    if fresh_auth_blockers or (
        account_row is not None and not account_row.get("fresh_story_auth_ok", False)
    ):
        return _error_response(
            "fresh_story_auth_required",
            409,
            message="Fresh story auth probe required within TTL window.",
            extra={
                "live_only_blockers": fresh_auth_blockers or live_only,
                "fresh_story_auth_ok": account_row.get("fresh_story_auth_ok") if account_row else False,
            },
        )

    live_blockers = list(report.get("live_blockers") or [])
    if live_blockers:
        return _error_response(
            "story_precheck_failed",
            409,
            message="Story rotation precheck blockers remain.",
            extra={"live_blockers": live_blockers},
        )

    if not report.get("live_publish_allowed"):
        gate_blockers = list(report.get("live_gate_blockers") or [])
        return _error_response(
            "story_precheck_failed",
            409,
            message="Controlled live gate blockers remain.",
            live_gate_blockers=gate_blockers,
        )

    return None, None


async def _execute_controlled_live_story_run(
    payload: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    from src.clients.manager import client_manager
    from src.stories.mention_plan import (
        extract_approved_mention_plan,
        normalize_mention_plan,
        require_all_mentions_flag,
    )
    from src.stories.publisher import story_publisher

    account_id = CONTROLLED_LIVE_ACCOUNT_ID
    media_path = str((report.get("media") or {}).get("path") or payload.get("media_path") or "").strip()
    caption = payload.get("caption")
    mentions_per_story = int(payload.get("mentions_per_story") or 0)
    mention_source_chat_id = payload.get("mention_source_chat_id", payload.get("mention_source"))
    mention_source_chat_id = (
        int(mention_source_chat_id)
        if mention_source_chat_id not in (None, "", "null")
        else None
    )
    # Controlled canaries default to fail-closed on mention loss.
    require_all = require_all_mentions_flag(payload, default=True)

    approved = extract_approved_mention_plan(payload)
    if approved is not None:
        # Exact Dry Run plan — never reselect.
        candidates = normalize_mention_plan(approved)[: max(0, mentions_per_story)]
        selection_source = "approved_payload"
    elif mentions_per_story > 0 and require_all:
        return {
            "ok": False,
            "published": False,
            "error": "story_mention_plan_mismatch",
            "message": (
                "Controlled live runs with mentions require selected_mention_candidates "
                "from Dry Run. Live execution will not silently reselect."
            ),
            "mentions_requested": mentions_per_story,
            "mentions_selected": [],
            "mentions_applied": [],
            "mentions_skipped": [],
            "controlled_live_account_id": CONTROLLED_LIVE_ACCOUNT_ID,
        }
    else:
        # Legacy / optional-mention path may use precheck selection.
        candidates = normalize_mention_plan(report.get("selected_mention_candidates") or [])[
            : max(0, mentions_per_story)
        ]
        selection_source = "precheck_reselect"

    mention_plan = candidates
    if mentions_per_story > 0 and require_all and len(mention_plan) < mentions_per_story:
        return {
            "ok": False,
            "published": False,
            "error": "story_mention_candidate_missing",
            "message": "Approved mention plan is incomplete for the requested mention count.",
            "mentions_requested": mentions_per_story,
            "mentions_selected": [
                {"username": c.get("username"), "peer_id": c.get("user_id")} for c in mention_plan
            ],
            "mentions_applied": [],
            "mentions_skipped": [],
            "controlled_live_account_id": CONTROLLED_LIVE_ACCOUNT_ID,
        }

    with get_db_context() as db:
        run = StoryRun(
            mode="once",
            media_path=media_path,
            caption=caption,
            mentions_per_story=mentions_per_story,
            max_stories=1,
            mention_source_chat_id=mention_source_chat_id,
            mention_plan=mention_plan,
            status="running",
            started_at=datetime.utcnow(),
            last_tick_at=datetime.utcnow(),
        )
        db.add(run)
        db.flush()
        run_id = int(run.id)

    from src.core.execution_guard import (
        ACTION_STORY_PUBLISH,
        guard_blocked_story_publish,
        require_execution_allowed,
    )

    blocked = require_execution_allowed(ACTION_STORY_PUBLISH, account_id=int(account_id))
    if blocked is not None:
        logger.warning(
            "controlled_live_run_blocked_execution_guard",
            run_id=run_id,
            account_id=int(account_id),
            reason=blocked.reason_code,
        )
        payload_blocked = guard_blocked_story_publish(blocked)
        return _finalize_run(
            run_id=run_id,
            account_id=account_id,
            success=False,
            error=str(payload_blocked.get("error") or blocked.reason_code),
            publish_result=payload_blocked,
            mention_plan=mention_plan,
        )

    logger.info(
        "controlled_story_run_started",
        run_id=run_id,
        account_id=account_id,
        media_path=media_path,
        mentions=len(mention_plan),
        mention_selection_source=selection_source,
        require_all_mentions=require_all,
    )

    client_wrapper, conn_err = await client_manager.connect_account(account_id)
    if not client_wrapper:
        return _finalize_run(
            run_id=run_id,
            account_id=account_id,
            success=False,
            error=conn_err or "client_unavailable",
            publish_result=None,
            mention_plan=mention_plan,
        )

    publish_result = await story_publisher.publish_story(
        client_wrapper=client_wrapper,
        media_path=media_path,
        caption=caption,
        mentions=mention_plan or None,
        require_all_mentions=require_all,
    )

    if publish_result.get("success"):
        return _finalize_run(
            run_id=run_id,
            account_id=account_id,
            success=True,
            error=None,
            publish_result=publish_result,
            mention_plan=mention_plan,
        )

    return _finalize_run(
        run_id=run_id,
        account_id=account_id,
        success=False,
        error=str(publish_result.get("error") or "publish_failed"),
        publish_result=publish_result,
        mention_plan=mention_plan,
    )


def _finalize_run(
    *,
    run_id: int,
    account_id: int,
    success: bool,
    error: str | None,
    publish_result: dict[str, Any] | None,
    mention_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    publish_result = publish_result or {}
    mention_fields = {
        "mentions_requested": publish_result.get("mentions_requested"),
        "mentions_selected": publish_result.get("mentions_selected")
        or [
            {"username": c.get("username"), "peer_id": c.get("user_id")}
            for c in (mention_plan or [])
        ],
        "mentions_applied": publish_result.get("mentions_applied") or [],
        "mentions_skipped": publish_result.get("mentions_skipped") or [],
        "mention_skip_reasons": publish_result.get("mention_skip_reasons") or [],
        "mentions": publish_result.get("mentions") or [],
    }
    if mention_fields["mentions_requested"] is None:
        mention_fields["mentions_requested"] = len(mention_plan or [])

    with get_db_context() as db:
        run = db.get(StoryRun, run_id)
        if run is None:
            raise RuntimeError(f"StoryRun {run_id} missing after creation")

        if mention_plan is not None and getattr(run, "mention_plan", None) in (None, [], {}):
            run.mention_plan = mention_plan

        step = StoryRunStep(
            run_id=run_id,
            account_id=account_id,
            story_id=publish_result.get("db_id"),
            status="ok" if success else "failed",
            error=error,
            executed_at=now,
        )
        db.add(step)

        if success:
            run.stories_ok = 1
            run.status = "completed"
        else:
            run.stories_failed = 1
            run.status = "failed"
        run.completed_at = now
        run.last_tick_at = now

        audit_details: dict[str, Any] = {
            "schema": "controlled_live_story_run_v1",
            "run_id": run_id,
            "account_id": account_id,
            "success": success,
            "error": error,
            "telegram_story_id": publish_result.get("story_id"),
            "db_story_id": publish_result.get("db_id"),
            "mention_plan": mention_plan or [],
            **mention_fields,
            "caption_sent": publish_result.get("caption_sent"),
        }
        db.add(
            SystemLog(
                level="INFO" if success else "ERROR",
                component="controlled_live_story_run",
                message="controlled_story_run_completed" if success else "controlled_story_run_failed",
                details=audit_details,
            )
        )
        db.commit()

    if success:
        logger.info(
            "controlled_story_run_completed",
            run_id=run_id,
            account_id=account_id,
            telegram_story_id=publish_result.get("story_id"),
            db_story_id=publish_result.get("db_id"),
            mentions_applied=len(mention_fields["mentions_applied"]),
        )
        return {
            "ok": True,
            "run_id": run_id,
            "account_id": account_id,
            "published": True,
            "story_id": publish_result.get("story_id"),
            "db_id": publish_result.get("db_id"),
            "message": publish_result.get("message") or "Story published.",
            "warning": publish_result.get("warning"),
            "controlled_live_account_id": CONTROLLED_LIVE_ACCOUNT_ID,
            **mention_fields,
        }

    logger.info(
        "controlled_story_run_failed",
        run_id=run_id,
        account_id=account_id,
        error=error,
    )
    return {
        "ok": False,
        "run_id": run_id,
        "account_id": account_id,
        "published": False,
        "error": publish_result.get("error") or "controlled_story_run_failed",
        "message": error or "Publish failed.",
        "controlled_live_account_id": CONTROLLED_LIVE_ACCOUNT_ID,
        **mention_fields,
    }


def controlled_live_run_http_response(payload: dict[str, Any] | None) -> tuple[Any, int]:
    """Flask handler entry: evaluate gates then optionally publish one story."""
    payload = payload or {}
    with get_db_context() as db:
        report = build_story_rotation_precheck(db, payload)

    err_body, status = evaluate_controlled_live_run_gates(payload, report)
    if err_body is not None and status is not None:
        return jsonify(err_body), status

    try:
        result = asyncio.run(_execute_controlled_live_story_run(payload, report))
    except Exception as exc:
        logger.exception("controlled_story_run_exception", error=str(exc))
        body, code = _error_response(
            "controlled_story_run_failed",
            500,
            message=str(exc),
        )
        return jsonify(body), code

    if result.get("ok"):
        return jsonify(result), 200
    return jsonify(result), 500
