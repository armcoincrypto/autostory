"""
Controlled autonomous AI Agent loop: optional sync → draft → optional send.

Cold start (autonomous + draft + no outbound): generate and send first message
without sync_inbound so a busy Telegram session lock does not block the opener.

Safety: one task = one target; respects max_messages, delays, and stop conditions.
Never logs session strings, API keys, or message bodies in audit detail.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import account_id_permitted_for_ai_agent_tasks
from src.ai_agent.task_target_dedupe import find_newer_active_duplicate, normalize_ai_target
from src.ai_agent.service import (
    AiAgentService,
    _append_audit,
    _latest_facts_payload,
    autonomous_operator_send_cooldown_seconds,
    autonomous_outbound_is_duplicate_of_recent_sent,
    autonomous_send_cooldown_remaining_sec,
    autonomous_should_wait_for_counterparty_reply,
    count_operator_outbound_sent,
    latest_operator_outbound_sent_at,
    latest_sendable_outbound_draft,
)
from src.core.ai_agent_models import AiAgentMessage, AiAgentTask

AUTO_MODES = frozenset({"off", "supervised", "autonomous"})

logger = structlog.get_logger(__name__)


def _has_any_outbound(db: Session, task_id: int) -> bool:
    return (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == task_id,
            AiAgentMessage.direction.in_(("out", "outbound")),
        )
        .first()
    ) is not None


class AiAgentAutoLoop:
    """
    Per-task auto processor. Call ``process_task(db, task_id)`` from a background worker
    inside ``get_db_context()`` so each task commits independently.
    """

    def __init__(self, service: Optional[AiAgentService] = None):
        self._svc = service or AiAgentService()

    def _audit(
        self,
        db: Session,
        action: str,
        task: AiAgentTask,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        _append_audit(
            db,
            action=action,
            task_id=task.id,
            account_id=task.account_id,
            detail=detail,
        )

    def _skip_autonomous_send_for_cooldown(
        self,
        db: Session,
        task: AiAgentTask,
        mode: str,
        *,
        ignore_delay: bool,
        phase: str,
    ) -> Optional[dict[str, Any]]:
        """
        After at least one operator outbound send, autonomous mode spaces further sends
        by a deterministic 60–300s window (see service.autonomous_send_cooldown_remaining_sec).
        Does not apply when ignore_delay=True (operator run-now).
        """
        if mode != "autonomous" or ignore_delay:
            return None
        check_at = datetime.utcnow()
        rem = autonomous_send_cooldown_remaining_sec(db, int(task.id), check_at)
        if rem <= 0:
            return None
        last_at = latest_operator_outbound_sent_at(db, int(task.id))
        cool = (
            autonomous_operator_send_cooldown_seconds(int(task.id), last_at)
            if last_at is not None
            else 0
        )
        logger.info(
            "ai_agent_send_skipped_cooldown",
            task_id=int(task.id),
            phase=phase,
            cooldown_sec=int(cool),
            seconds_remaining=round(rem, 3),
        )
        task.auto_last_run_at = check_at
        db.flush()
        return {
            "outcome": "skipped",
            "reason": "send_cooldown",
            "phase": phase,
            "cooldown_sec": int(cool),
            "seconds_remaining": rem,
        }

    def _skip_waiting_for_counterparty_reply(
        self,
        db: Session,
        task: AiAgentTask,
        task_id: int,
        *,
        ignore_delay: bool,
        phase: str,
    ) -> Optional[dict[str, Any]]:
        if ignore_delay:
            return None
        if not autonomous_should_wait_for_counterparty_reply(db, task_id):
            return None
        logger.info(
            "ai_agent_skip_waiting_for_counterparty_reply",
            task_id=int(task_id),
            phase=phase,
        )
        self._audit(
            db,
            "waiting_for_counterparty_reply",
            task,
            {"phase": phase},
        )
        task.auto_last_run_at = datetime.utcnow()
        db.flush()
        return {
            "outcome": "skipped",
            "reason": "waiting_for_counterparty_reply",
            "phase": phase,
        }

    def _skip_duplicate_autonomous_outbound(
        self,
        db: Session,
        task: AiAgentTask,
        task_id: int,
        draft_body: Optional[str],
        *,
        ignore_delay: bool,
        phase: str,
    ) -> Optional[dict[str, Any]]:
        if ignore_delay:
            return None
        if not autonomous_outbound_is_duplicate_of_recent_sent(db, task_id, draft_body):
            return None
        logger.info(
            "ai_agent_skip_duplicate_message",
            task_id=int(task_id),
            phase=phase,
        )
        self._audit(
            db,
            "duplicate_outbound_skipped",
            task,
            {"phase": phase},
        )
        task.auto_last_run_at = datetime.utcnow()
        db.flush()
        return {
            "outcome": "skipped",
            "reason": "duplicate_outbound_skipped",
            "phase": phase,
        }

    def process_task(
        self,
        db: Session,
        task_id: int,
        *,
        ignore_delay: bool = False,
    ) -> dict[str, Any]:
        """
        Run one auto-loop iteration. Returns a small status dict (session_lock flags
        cooldown in the background worker; skipped_delay for run-now when delay applies).
        """
        task = db.get(AiAgentTask, task_id)
        if not task:
            return {"outcome": "missing_task"}

        if not account_id_permitted_for_ai_agent_tasks(db, int(task.account_id)):
            logger.info(
                "ai_agent_auto_loop_skipped_non_reserved_account",
                task_id=int(task.id),
                account_id=int(task.account_id),
            )
            return {
                "outcome": "skipped",
                "reason": "account_not_reserved_for_ai_agent",
            }

        mode = (getattr(task, "auto_mode", None) or "off").strip().lower()
        if mode not in AUTO_MODES:
            mode = "off"
        if mode == "off":
            return {"outcome": "skipped", "reason": "auto_mode_off"}

        if task.status in ("paused", "completed", "failed", "cancelled"):
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "task_status", "status": task.status},
            )
            db.flush()
            return {"outcome": "skipped", "reason": "task_status", "status": task.status}

        newer = find_newer_active_duplicate(db, task)
        if newer is not None:
            pause_at = datetime.utcnow()
            task.status = "paused"
            task.updated_at = pause_at
            task.last_activity_at = pause_at
            nt = normalize_ai_target(task.target_username_or_id)
            self._audit(
                db,
                "superseded_by_newer_task",
                task,
                {"newer_task_id": int(newer.id), "normalized_target": nt},
            )
            logger.info(
                "ai_agent_task_superseded",
                old_task_id=int(task.id),
                newer_task_id=int(newer.id),
                normalized_target=nt,
            )
            db.flush()
            return {
                "outcome": "skipped",
                "reason": "superseded_by_newer_task",
                "newer_task_id": int(newer.id),
            }

        stage = (getattr(task, "negotiation_stage", None) or "").strip().lower()
        if stage == "completed":
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "negotiation_stage_completed"},
            )
            db.flush()
            return {"outcome": "skipped", "reason": "negotiation_stage_completed"}

        if stage in ("ready_for_operator", "risky"):
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "negotiation_stage_requires_operator", "stage": stage},
            )
            db.flush()
            return {"outcome": "skipped", "reason": "negotiation_stage_requires_operator"}

        if task.final_summary and str(task.final_summary).strip():
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "task_final_summary_set"},
            )
            db.flush()
            return {"outcome": "skipped", "reason": "task_final_summary_set"}

        facts = _latest_facts_payload(db, task_id)
        if isinstance(facts, dict):
            fs_ai = facts.get("final_summary")
            if fs_ai is not None and str(fs_ai).strip():
                self._audit(
                    db,
                    "auto_loop_skipped_reason",
                    task,
                    {"reason": "facts_final_summary_present"},
                )
                db.flush()
                return {"outcome": "skipped", "reason": "facts_final_summary_present"}

        risks: list[Any] = []
        if isinstance(facts, dict):
            rf = facts.get("risk_flags")
            if isinstance(rf, list):
                risks = rf
        if risks:
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "risk_flags_present", "count": len(risks)},
            )
            db.flush()
            return {"outcome": "skipped", "reason": "risk_flags_present"}

        sent_n = count_operator_outbound_sent(db, task_id)
        cap = int(task.max_messages or 0)
        if cap >= 1 and sent_n >= cap:
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "max_messages", "sent": sent_n, "cap": cap},
            )
            db.flush()
            return {"outcome": "skipped", "reason": "max_messages"}

        now = datetime.utcnow()
        delay = int(getattr(task, "auto_delay_sec", None) or 20)
        delay = max(10, min(delay, 3600))
        last_run = getattr(task, "auto_last_run_at", None)
        if last_run is not None and (now - last_run).total_seconds() < float(delay):
            if not ignore_delay:
                elapsed = (now - last_run).total_seconds()
                rem = max(0.0, float(delay) - elapsed)
                return {
                    "outcome": "skipped_delay",
                    "seconds_remaining": rem,
                    "delay_sec": float(delay),
                }

        # First outbound: do not require Telegram read/sync (session may be busy elsewhere).
        if (
            mode == "autonomous"
            and task.status == "draft"
            and not _has_any_outbound(db, task_id)
        ):
            gen_cs = self._svc.generate_draft(db, task_id)
            if isinstance(gen_cs, tuple):
                err, _status = gen_cs
                self._audit(
                    db,
                    "auto_loop_skipped_reason",
                    task,
                    {
                        "reason": "generate_failed",
                        "error": err.get("error"),
                        "phase": "cold_start",
                    },
                )
                task.auto_last_run_at = now
                db.flush()
                return {"outcome": "executed", "phase": "cold_start", "generate_failed": True}

            skip_wait_cs = self._skip_waiting_for_counterparty_reply(
                db, task, task_id, ignore_delay=ignore_delay, phase="cold_start"
            )
            if skip_wait_cs is not None:
                return skip_wait_cs
            dm_cs = gen_cs.get("message")
            skip_dup_cs = self._skip_duplicate_autonomous_outbound(
                db,
                task,
                task_id,
                dm_cs.body if dm_cs else None,
                ignore_delay=ignore_delay,
                phase="cold_start",
            )
            if skip_dup_cs is not None:
                return skip_dup_cs

            skip_send = self._skip_autonomous_send_for_cooldown(
                db, task, mode, ignore_delay=ignore_delay, phase="cold_start"
            )
            if skip_send is not None:
                return skip_send

            appr_cs = self._svc.approve_send(db, task_id)
            if isinstance(appr_cs, tuple):
                err, _st = appr_cs
                self._audit(
                    db,
                    "auto_loop_skipped_reason",
                    task,
                    {
                        "reason": "approve_send_blocked",
                        "error": err.get("error"),
                        "phase": "cold_start",
                    },
                )
                task.auto_last_run_at = now
                db.flush()
                return {"outcome": "executed", "phase": "cold_start", "blocked": True}

            session_lock = False
            if appr_cs.get("ok"):
                self._audit(
                    db,
                    "auto_loop_cold_start_sent",
                    task,
                    {
                        "telegram_message_id": appr_cs.get("telegram_message_id"),
                        "message_row_id": appr_cs.get("message").id
                        if appr_cs.get("message")
                        else None,
                        "draft_message_id": gen_cs.get("message").id
                        if gen_cs.get("message")
                        else None,
                    },
                )
            elif appr_cs.get("transient"):
                session_lock = True
                self._audit(
                    db,
                    "auto_loop_cold_start_transient",
                    task,
                    {
                        "error_code": appr_cs.get("error_code"),
                    },
                )
            else:
                self._audit(
                    db,
                    "auto_loop_skipped_reason",
                    task,
                    {
                        "reason": "approve_send_failed",
                        "code": appr_cs.get("error_code"),
                        "phase": "cold_start",
                    },
                )
            db.refresh(task)
            task.auto_last_run_at = datetime.utcnow()
            db.flush()
            return {
                "outcome": "executed",
                "phase": "cold_start",
                "sent": bool(appr_cs.get("ok")),
                "session_lock": session_lock,
                "transient": bool(appr_cs.get("transient")),
                "error_code": appr_cs.get("error_code"),
            }

        if mode == "autonomous" and task.status == "waiting_admin_approval":
            drow = latest_sendable_outbound_draft(db, task_id)
            if drow is not None:
                skip_wait_ex = self._skip_waiting_for_counterparty_reply(
                    db,
                    task,
                    task_id,
                    ignore_delay=ignore_delay,
                    phase="approve_existing_draft",
                )
                if skip_wait_ex is not None:
                    return skip_wait_ex
                skip_dup_ex = self._skip_duplicate_autonomous_outbound(
                    db,
                    task,
                    task_id,
                    drow.body,
                    ignore_delay=ignore_delay,
                    phase="approve_existing_draft",
                )
                if skip_dup_ex is not None:
                    return skip_dup_ex

                skip_send = self._skip_autonomous_send_for_cooldown(
                    db,
                    task,
                    mode,
                    ignore_delay=ignore_delay,
                    phase="approve_existing_draft",
                )
                if skip_send is not None:
                    return skip_send

                appr = self._svc.approve_send(db, task_id)
                if isinstance(appr, tuple):
                    err, _st = appr
                    self._audit(
                        db,
                        "auto_loop_skipped_reason",
                        task,
                        {
                            "reason": "approve_send_blocked",
                            "error": err.get("error"),
                        },
                    )
                    task.auto_last_run_at = now
                    db.flush()
                    return {"outcome": "executed", "phase": "approve_existing", "blocked": True}

                session_lock = bool(appr.get("transient"))
                if appr.get("ok"):
                    self._audit(
                        db,
                        "auto_loop_sent_existing_draft",
                        task,
                        {
                            "telegram_message_id": appr.get("telegram_message_id"),
                            "message_row_id": appr.get("message").id
                            if appr.get("message")
                            else None,
                            "draft_message_id": drow.id,
                        },
                    )
                elif appr.get("transient"):
                    self._audit(
                        db,
                        "auto_loop_skipped_reason",
                        task,
                        {
                            "reason": "transient_session_busy",
                            "error_code": appr.get("error_code"),
                            "phase": "approve_existing_draft",
                        },
                    )
                else:
                    self._audit(
                        db,
                        "auto_loop_skipped_reason",
                        task,
                        {
                            "reason": "approve_send_failed",
                            "code": appr.get("error_code"),
                        },
                    )
                db.refresh(task)
                task.auto_last_run_at = datetime.utcnow()
                db.flush()
                return {
                    "outcome": "executed",
                    "phase": "approve_existing_draft",
                    "sent": bool(appr.get("ok")),
                    "session_lock": session_lock,
                    "error_code": appr.get("error_code"),
                }

        sync_res = self._svc.sync_inbound(
            db,
            task_id,
            apply_autonomous_inbound_fetch_cooldown=(
                mode == "autonomous" and not ignore_delay
            ),
        )
        if isinstance(sync_res, dict) and sync_res.get("fetch_skipped_cooldown"):
            task.auto_last_run_at = now
            db.flush()
            return {
                "outcome": "skipped",
                "reason": "fetch_cooldown",
                "phase": "sync_inbound",
                "seconds_remaining": sync_res.get("seconds_remaining"),
            }
        if isinstance(sync_res, dict) and sync_res.get("inbound_fetch_disabled"):
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "inbound_fetch_not_allowed", "phase": "sync_inbound"},
            )
            task.auto_last_run_at = now
            db.flush()
            return {
                "outcome": "skipped",
                "reason": "inbound_fetch_not_allowed",
                "phase": "sync_inbound",
            }
        if isinstance(sync_res, dict) and sync_res.get("transient_telegram_busy"):
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {
                    "reason": "transient_session_busy",
                    "error_code": sync_res.get("error_code"),
                    "phase": "sync_inbound",
                },
            )
            task.auto_last_run_at = now
            db.flush()
            return {
                "outcome": "executed",
                "phase": "sync_inbound",
                "session_lock": True,
                "error_code": sync_res.get("error_code"),
            }
        if isinstance(sync_res, tuple):
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {
                    "reason": "sync_failed",
                    "error": sync_res[0].get("error"),
                    "code": sync_res[0].get("error_code"),
                },
            )
            task.auto_last_run_at = now
            db.flush()
            return {"outcome": "executed", "phase": "sync_inbound", "sync_failed": True}

        inbound_ins = int(sync_res.get("inbound_inserted") or 0)
        has_out = _has_any_outbound(db, task_id)

        # First outreach: no outbound yet — still generate (and send if autonomous).
        if inbound_ins == 0 and has_out and not ignore_delay:
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "no_new_inbound"},
            )
            task.auto_last_run_at = now
            db.flush()
            return {"outcome": "skipped", "reason": "no_new_inbound"}

        skip_wait_gen = self._skip_waiting_for_counterparty_reply(
            db, task, task_id, ignore_delay=ignore_delay, phase="generate_draft"
        )
        if skip_wait_gen is not None:
            return skip_wait_gen

        gen = self._svc.generate_draft(db, task_id)
        if isinstance(gen, tuple):
            err, _status = gen
            self._audit(
                db,
                "auto_loop_skipped_reason",
                task,
                {"reason": "generate_failed", "error": err.get("error")},
            )
            task.auto_last_run_at = now
            db.flush()
            return {"outcome": "executed", "phase": "generate", "generate_failed": True}

        db.refresh(task)
        task.auto_last_run_at = datetime.utcnow()
        db.flush()

        self._audit(
            db,
            "auto_loop_run",
            task,
            {
                "mode": mode,
                "inbound_inserted": inbound_ins,
                "draft_message_id": gen.get("message").id if gen.get("message") else None,
            },
        )
        db.flush()

        if mode == "supervised":
            return {"outcome": "executed", "phase": "generate_only", "supervised": True}

        if mode == "autonomous":
            skip_wait_ap = self._skip_waiting_for_counterparty_reply(
                db, task, task_id, ignore_delay=ignore_delay, phase="approve_after_generate"
            )
            if skip_wait_ap is not None:
                return skip_wait_ap
            gmsg = gen.get("message")
            skip_dup_ap = self._skip_duplicate_autonomous_outbound(
                db,
                task,
                task_id,
                gmsg.body if gmsg else None,
                ignore_delay=ignore_delay,
                phase="approve_after_generate",
            )
            if skip_dup_ap is not None:
                return skip_dup_ap

            skip_send = self._skip_autonomous_send_for_cooldown(
                db,
                task,
                mode,
                ignore_delay=ignore_delay,
                phase="approve_after_generate",
            )
            if skip_send is not None:
                return skip_send

            appr = self._svc.approve_send(db, task_id)
            if isinstance(appr, tuple):
                err, _st = appr
                self._audit(
                    db,
                    "auto_loop_skipped_reason",
                    task,
                    {"reason": "approve_send_blocked", "error": err.get("error")},
                )
                db.flush()
                return {"outcome": "executed", "phase": "approve_after_generate", "blocked": True}

            session_lock = bool(appr.get("transient"))
            if appr.get("ok"):
                self._audit(
                    db,
                    "auto_loop_sent",
                    task,
                    {
                        "telegram_message_id": appr.get("telegram_message_id"),
                        "message_row_id": appr.get("message").id
                        if appr.get("message")
                        else None,
                    },
                )
            elif appr.get("transient"):
                self._audit(
                    db,
                    "auto_loop_skipped_reason",
                    task,
                    {
                        "reason": "transient_session_busy",
                        "error_code": appr.get("error_code"),
                        "phase": "approve_after_generate",
                    },
                )
            else:
                self._audit(
                    db,
                    "auto_loop_skipped_reason",
                    task,
                    {
                        "reason": "approve_send_failed",
                        "code": appr.get("error_code"),
                    },
                )
            db.refresh(task)
            task.auto_last_run_at = datetime.utcnow()
            db.flush()
            return {
                "outcome": "executed",
                "phase": "approve_after_generate",
                "sent": bool(appr.get("ok")),
                "session_lock": session_lock,
                "error_code": appr.get("error_code"),
            }

        return {"outcome": "skipped", "reason": "mode_done"}
