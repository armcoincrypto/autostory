"""Controlled multi-account live story run (allowlist-gated).

Live mutations stay on this path only. Legacy ``/api/stories/publish`` and
``/batch`` remain blocked by the p3 gate.

Account scope:
- Prefer ``STORY_ACCOUNT_MUTATION_ALLOWLIST``
- Legacy fallback: single ``CONTROLLED_STORY_ACCOUNT_ID`` when allowlist is empty
"""
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
    MULTI_ACCOUNT_CONFIRMATION_TOKEN,
    allocate_mentions_without_replacement,
    build_story_rotation_precheck,
    controlled_live_account_id,
    live_confirmation_token_for_accounts,
    mutation_allowlist_account_ids,
)
from src.stories.scheduler_integration import controlled_story_accounts_execution_allowed

logger = structlog.get_logger(__name__)


def live_confirmation_token(account_id: int | None = None) -> str:
    aid = int(account_id) if account_id is not None else controlled_live_account_id()
    return f"LIVE_STORY_ACCOUNT_{aid}"


# Backward-compatible alias for tests/importers (default account 140).
LIVE_CONFIRMATION_TOKEN = live_confirmation_token(CONTROLLED_LIVE_ACCOUNT_ID)


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
        "controlled_live_account_id": controlled_live_account_id(),
        "mutation_allowlist_account_ids": sorted(mutation_allowlist_account_ids()),
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
    from src.stories.mutation_boundary import story_mutations_enabled

    if not story_mutations_enabled():
        return _error_response(
            "story_mutations_disabled",
            403,
            message="STORY_MUTATIONS_ENABLED is false/missing; Story mutations denied.",
        )

    if payload.get("dry_run"):
        return _error_response(
            "story_precheck_failed",
            409,
            message="Live runs reject dry_run=true. Use POST /api/stories/dry-run instead.",
        )

    requested = _requested_account_ids(payload)
    if not requested:
        return _error_response(
            "controlled_account_required",
            403,
            message="account_ids is required.",
            live_gate_blockers=["account_ids_required"],
        )

    # Dedupe while preserving order.
    seen: set[int] = set()
    ordered: list[int] = []
    for aid in requested:
        if aid in seen:
            continue
        seen.add(aid)
        ordered.append(aid)
    requested = ordered

    allowlist = mutation_allowlist_account_ids()
    from src.stories.autostory_hardening import account_in_active_wave_authorization

    not_on_allowlist = [
        aid
        for aid in requested
        if aid not in allowlist and not account_in_active_wave_authorization(aid)
    ]
    if not_on_allowlist:
        return _error_response(
            "controlled_account_required",
            403,
            message="One or more account_ids are not on the mutation allowlist "
            "or an active campaign wave authorization.",
            live_gate_blockers=["live_gate_accounts_not_on_allowlist"],
            extra={"denied_account_ids": not_on_allowlist},
        )

    exec_ok, exec_reason, denied = controlled_story_accounts_execution_allowed(requested)
    if not exec_ok:
        return _error_response(
            exec_reason,
            403,
            message="Controlled Story execution is disabled or accounts are not on the allowlist.",
            live_gate_blockers=[exec_reason],
            extra={"denied_account_ids": denied},
        )

    # Legacy single-canary: when allowlist env is unset, require CONTROLLED_STORY_ACCOUNT_ID match.
    env_allowlist = (os.getenv("STORY_ACCOUNT_MUTATION_ALLOWLIST") or "").strip()
    if not env_allowlist and len(requested) == 1:
        aid = requested[0]
        env_account = os.getenv("CONTROLLED_STORY_ACCOUNT_ID", "").strip()
        if env_account != str(aid):
            return _error_response(
                "controlled_account_flag_required",
                403,
                message=f"Set CONTROLLED_STORY_ACCOUNT_ID={aid} for this path.",
            )

    if payload.get("explicit_operator_approval") is not True:
        return _error_response(
            "operator_approval_required",
            403,
            message="Set explicit_operator_approval=true after operator sign-off.",
            live_gate_blockers=["explicit_operator_approval_required"],
        )

    expected_token = live_confirmation_token_for_accounts(requested)
    confirmation = str(payload.get("confirmation_token") or "").strip()
    accepted = {expected_token, MULTI_ACCOUNT_CONFIRMATION_TOKEN}
    if len(requested) == 1:
        accepted.add(live_confirmation_token(requested[0]))
    if confirmation not in accepted:
        return _error_response(
            "confirmation_token_required",
            403,
            message=f"confirmation_token must be {expected_token}.",
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

    account_rows = {int(row.get("account_id") or 0): row for row in (report.get("accounts") or [])}
    for aid in requested:
        account_row = account_rows.get(aid)
        if account_row and account_row.get("blockers"):
            return _error_response(
                "story_precheck_failed",
                409,
                message=f"Account {aid} precheck blockers remain.",
                extra={
                    "account_id": aid,
                    "blockers": account_row.get("blockers"),
                    "live_gate_blockers": report.get("live_gate_blockers", []),
                },
            )
        if account_row is not None and not account_row.get("fresh_story_auth_ok", False):
            return _error_response(
                "fresh_story_auth_required",
                409,
                message=f"Fresh story auth probe required for account {aid}.",
                extra={
                    "account_id": aid,
                    "fresh_story_auth_ok": False,
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

    eligible = [int(x) for x in (report.get("eligible_accounts") or [])]
    if set(requested) - set(eligible):
        return _error_response(
            "story_precheck_failed",
            409,
            message="One or more selected accounts are not Story-ready.",
            extra={
                "requested_account_ids": requested,
                "eligible_accounts": eligible,
            },
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


async def _publish_one_account(
    *,
    run_id: int,
    account_id: int,
    media_path: str,
    caption: Any,
    mention_plan: list[dict[str, Any]],
    require_all: bool,
) -> dict[str, Any]:
    from src.core.execution_guard import (
        ACTION_STORY_PUBLISH,
        guard_blocked_story_publish,
        require_execution_allowed,
    )
    from src.stories.client_lifecycle import open_controlled_story_client
    from src.stories.publisher import story_publisher

    blocked = require_execution_allowed(
        ACTION_STORY_PUBLISH,
        account_id=int(account_id),
        scope="controlled_live",
    )
    if blocked is not None:
        logger.warning(
            "controlled_live_run_blocked_execution_guard",
            run_id=run_id,
            account_id=int(account_id),
            reason=blocked.reason_code,
        )
        payload_blocked = guard_blocked_story_publish(blocked)
        return {
            "account_id": account_id,
            "success": False,
            "error": str(payload_blocked.get("error") or blocked.reason_code),
            "publish_result": payload_blocked,
            "mention_plan": mention_plan,
        }

    lease, conn_err = await open_controlled_story_client(account_id)
    if lease is None:
        return {
            "account_id": account_id,
            "success": False,
            "error": conn_err or "client_unavailable",
            "publish_result": None,
            "mention_plan": mention_plan,
        }

    try:
        publish_result = await story_publisher.publish_story(
            client_wrapper=lease.wrapper,
            media_path=media_path,
            caption=caption,
            mentions=mention_plan or None,
            require_all_mentions=require_all,
            execution_scope="controlled_live",
        )
    except Exception as exc:
        publish_result = {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        cleanup = await lease.close()

    return {
        "account_id": account_id,
        "success": bool(publish_result.get("success")),
        "error": (
            None
            if publish_result.get("success")
            else str(publish_result.get("error") or "publish_failed")
        ),
        "publish_result": publish_result,
        "mention_plan": mention_plan,
        "cleanup": cleanup,
    }


async def _execute_controlled_live_story_run(
    payload: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    from src.stories.mention_plan import (
        extract_approved_mention_plan,
        normalize_mention_plan,
    )

    requested = _requested_account_ids(payload) or []
    seen: set[int] = set()
    account_ids: list[int] = []
    for aid in requested:
        if aid in seen:
            continue
        seen.add(aid)
        account_ids.append(aid)

    media_path = str((report.get("media") or {}).get("path") or payload.get("media_path") or "").strip()
    caption = payload.get("caption")
    mentions_per_story = int(payload.get("mentions_per_story") or 0)
    mention_source_chat_id = payload.get("mention_source_chat_id", payload.get("mention_source"))
    mention_source_chat_id = (
        int(mention_source_chat_id)
        if mention_source_chat_id not in (None, "", "null")
        else None
    )
    require_all = mentions_per_story > 0
    total_mentions_needed = mentions_per_story * len(account_ids)

    approved = extract_approved_mention_plan(payload)
    if approved is not None:
        candidates = normalize_mention_plan(approved)[: max(0, total_mentions_needed)]
        selection_source = "approved_payload"
    elif mentions_per_story > 0:
        return {
            "ok": False,
            "published": False,
            "error": "story_mention_plan_mismatch",
            "message": (
                "Controlled live runs with mentions require selected_mention_candidates "
                "from Dry Run. Live execution will not silently reselect."
            ),
            "mentions_requested": total_mentions_needed,
            "mentions_selected": [],
            "mentions_applied": [],
            "mentions_skipped": [],
            "account_ids": account_ids,
            "controlled_live_account_id": controlled_live_account_id(),
        }
    else:
        candidates = []
        selection_source = "zero_mentions"

    if mentions_per_story > 0 and require_all and len(candidates) < total_mentions_needed:
        return {
            "ok": False,
            "published": False,
            "error": "story_mention_candidate_missing",
            "message": "Approved mention plan is incomplete for the selected accounts.",
            "mentions_requested": total_mentions_needed,
            "mentions_selected": [
                {"username": c.get("username"), "peer_id": c.get("user_id")} for c in candidates
            ],
            "account_ids": account_ids,
            "controlled_live_account_id": controlled_live_account_id(),
        }

    per_account = allocate_mentions_without_replacement(
        candidates,
        account_ids=account_ids,
        mentions_per_story=mentions_per_story,
    )

    with get_db_context() as db:
        run = StoryRun(
            mode="once",
            media_path=media_path,
            caption=caption,
            mentions_per_story=mentions_per_story,
            max_stories=len(account_ids),
            mention_source_chat_id=mention_source_chat_id,
            mention_plan={
                "flat": candidates,
                "per_account": [
                    {"account_id": aid, "mentions": chunk}
                    for aid, chunk in zip(account_ids, per_account)
                ],
                "account_ids": account_ids,
            },
            status="running",
            started_at=datetime.utcnow(),
            last_tick_at=datetime.utcnow(),
        )
        db.add(run)
        db.flush()
        run_id = int(run.id)

    logger.info(
        "controlled_story_run_started",
        run_id=run_id,
        account_ids=account_ids,
        media_path=media_path,
        mentions=len(candidates),
        mention_selection_source=selection_source,
        require_all_mentions=require_all,
    )

    step_results: list[dict[str, Any]] = []
    for aid, mention_plan in zip(account_ids, per_account):
        step_results.append(
            await _publish_one_account(
                run_id=run_id,
                account_id=aid,
                media_path=media_path,
                caption=caption,
                mention_plan=mention_plan,
                require_all=require_all,
            )
        )

    return _finalize_multi_run(
        run_id=run_id,
        account_ids=account_ids,
        step_results=step_results,
        flat_mention_plan=candidates,
    )


def _finalize_multi_run(
    *,
    run_id: int,
    account_ids: list[int],
    step_results: list[dict[str, Any]],
    flat_mention_plan: list[dict[str, Any]],
) -> dict[str, Any]:
    now = datetime.utcnow()
    ok_count = 0
    fail_count = 0
    ambiguous = False
    steps_out: list[dict[str, Any]] = []

    with get_db_context() as db:
        run = db.get(StoryRun, run_id)
        if run is None:
            raise RuntimeError(f"StoryRun {run_id} missing after creation")

        for step in step_results:
            publish_result = step.get("publish_result") or {}
            success = bool(step.get("success"))
            step_ambiguous = bool(publish_result.get("ambiguous_no_retry"))
            if step_ambiguous:
                ambiguous = True
                fail_count += 1
                status = "ambiguous_no_retry"
            elif success:
                ok_count += 1
                status = "ok"
            else:
                fail_count += 1
                status = "failed"

            db.add(
                StoryRunStep(
                    run_id=run_id,
                    account_id=int(step["account_id"]),
                    story_id=publish_result.get("db_id"),
                    status=status,
                    error=step.get("error"),
                    executed_at=now,
                )
            )
            steps_out.append(
                {
                    "account_id": int(step["account_id"]),
                    "ok": success and not step_ambiguous,
                    "status": status,
                    "error": step.get("error"),
                    "story_id": publish_result.get("story_id"),
                    "db_id": publish_result.get("db_id"),
                    "mentions_applied": publish_result.get("mentions_applied") or [],
                    "cleanup": step.get("cleanup"),
                }
            )

        run.stories_ok = ok_count
        run.stories_failed = fail_count
        if ambiguous:
            run.status = "ambiguous_no_retry"
        elif fail_count == 0 and ok_count > 0:
            run.status = "completed"
        elif ok_count == 0:
            run.status = "failed"
        else:
            run.status = "completed_with_errors"
        run.completed_at = now
        run.last_tick_at = now

        db.add(
            SystemLog(
                level="WARNING" if ambiguous else ("INFO" if fail_count == 0 else "ERROR"),
                component="controlled_live_story_run",
                message="controlled_story_multi_run_finished",
                details={
                    "schema": "controlled_live_story_run_v2_multi",
                    "run_id": run_id,
                    "account_ids": account_ids,
                    "stories_ok": ok_count,
                    "stories_failed": fail_count,
                    "status": run.status,
                    "mention_plan": flat_mention_plan,
                    "steps": steps_out,
                },
            )
        )
        db.commit()

    overall_ok = fail_count == 0 and ok_count > 0 and not ambiguous
    result = {
        "ok": overall_ok,
        "run_id": run_id,
        "account_ids": account_ids,
        "account_id": account_ids[0] if len(account_ids) == 1 else None,
        "published": overall_ok if len(account_ids) == 1 else ok_count > 0,
        "stories_ok": ok_count,
        "stories_failed": fail_count,
        "steps": steps_out,
        "message": (
            f"Published {ok_count}/{len(account_ids)} stories."
            if ok_count
            else "No stories published."
        ),
        "controlled_live_account_id": controlled_live_account_id(),
        "mutation_allowlist_account_ids": sorted(mutation_allowlist_account_ids()),
        "mentions_requested": len(flat_mention_plan),
        "mentions_selected": [
            {"username": c.get("username"), "peer_id": c.get("user_id")}
            for c in flat_mention_plan
        ],
    }
    if ambiguous:
        result["result_classification"] = "AMBIGUOUS_NO_RETRY"
        result["error"] = "AMBIGUOUS_NO_RETRY"
        result["ok"] = False
    elif not overall_ok:
        result["error"] = "controlled_story_run_failed"
    return result


def controlled_live_run_http_response(payload: dict[str, Any] | None) -> tuple[Any, int]:
    """Flask handler entry: evaluate gates then publish one story per selected account."""
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
    if result.get("result_classification") == "AMBIGUOUS_NO_RETRY":
        return jsonify(result), 409
    return jsonify(result), 500
