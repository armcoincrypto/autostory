"""AI Agent service: drafts, Telegram sync/send, autonomous guards."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from config.settings import settings as app_settings

from src.ai_agent.ai_client import AiAgentClient
from src.ai_agent.telegram_single_sender import TelegramSingleSender
from src.core.ai_agent_models import AiAgentAudit, AiAgentMessage, AiAgentTask


def _norm_body(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def _append_audit(
    db: Session,
    *,
    action: str,
    task_id: int,
    account_id: int | None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        AiAgentAudit(
            task_id=int(task_id),
            account_id=account_id,
            action=action,
            detail_json=detail or {},
            created_at=datetime.utcnow(),
        )
    )


def _latest_facts_payload(db: Session, task_id: int) -> dict[str, Any]:
    rows = (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == int(task_id),
            AiAgentMessage.direction.in_(("out", "outbound")),
        )
        .order_by(AiAgentMessage.id.desc())
        .all()
    )
    for m in rows:
        meta = m.meta_json if isinstance(m.meta_json, dict) else {}
        facts = meta.get("extracted_facts")
        if isinstance(facts, dict):
            return dict(facts)
    return {}


def count_operator_outbound_sent(db: Session, task_id: int) -> int:
    return (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == int(task_id),
            AiAgentMessage.direction.in_(("out", "outbound")),
            AiAgentMessage.status == "sent",
        )
        .count()
    )


def latest_operator_outbound_sent_at(db: Session, task_id: int) -> datetime | None:
    m = (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == int(task_id),
            AiAgentMessage.direction.in_(("out", "outbound")),
            AiAgentMessage.status == "sent",
        )
        .order_by(AiAgentMessage.id.desc())
        .first()
    )
    return m.created_at if m else None


def latest_sendable_outbound_draft(db: Session, task_id: int) -> AiAgentMessage | None:
    return (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == int(task_id),
            AiAgentMessage.direction.in_(("out", "outbound")),
            AiAgentMessage.status == "draft",
        )
        .order_by(AiAgentMessage.id.desc())
        .first()
    )


def autonomous_operator_send_cooldown_seconds(task_id: int, last_at: datetime) -> int:
    payload = f"{int(task_id)}:{last_at.isoformat()}".encode()
    h = int(hashlib.sha256(payload).hexdigest()[:8], 16)
    return 60 + (h % 241)


def autonomous_send_cooldown_remaining_sec(
    db: Session, task_id: int, check_at: datetime
) -> float:
    last_at = latest_operator_outbound_sent_at(db, task_id)
    if last_at is None:
        return 0.0
    cool = float(autonomous_operator_send_cooldown_seconds(int(task_id), last_at))
    elapsed = (check_at - last_at).total_seconds()
    return max(0.0, cool - elapsed)


def autonomous_should_wait_for_counterparty_reply(db: Session, task_id: int) -> bool:
    last_out = (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == int(task_id),
            AiAgentMessage.direction.in_(("out", "outbound")),
            AiAgentMessage.status == "sent",
        )
        .order_by(AiAgentMessage.id.desc())
        .first()
    )
    last_in = (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == int(task_id),
            AiAgentMessage.direction.in_(("in", "inbound")),
        )
        .order_by(AiAgentMessage.id.desc())
        .first()
    )
    if last_out is None:
        return False
    if last_in is None:
        return True
    return last_out.created_at > last_in.created_at


def autonomous_outbound_is_duplicate_of_recent_sent(
    db: Session, task_id: int, draft_body: str | None
) -> bool:
    if not (draft_body or "").strip():
        return False
    last = (
        db.query(AiAgentMessage)
        .filter(
            AiAgentMessage.task_id == int(task_id),
            AiAgentMessage.direction.in_(("out", "outbound")),
            AiAgentMessage.status == "sent",
        )
        .order_by(AiAgentMessage.id.desc())
        .first()
    )
    if last is None:
        return False
    return _norm_body(last.body) == _norm_body(draft_body)


def _history_from_messages(msgs: list[AiAgentMessage]) -> list[dict[str, str]]:
    hist: list[dict[str, str]] = []
    for m in sorted(msgs, key=lambda x: int(x.id or 0)):
        d = (m.direction or "").lower()
        if d in ("in", "inbound"):
            hist.append({"role": "user", "content": m.body or ""})
        elif d in ("out", "outbound") and (m.status or "").lower() == "sent":
            hist.append({"role": "assistant", "content": m.body or ""})
    return hist


def _strip_tg_id(raw: dict[str, Any]) -> Any:
    return raw.get("telegram_message_id") or raw.get("id")


class AiAgentService:
    def __init__(
        self,
        sender: Any | None = None,
        client: AiAgentClient | None = None,
    ) -> None:
        self._sender = sender if sender is not None else TelegramSingleSender()
        self._client = client if client is not None else AiAgentClient()

    def generate_draft(self, db: Session, task_id: int) -> dict[str, Any] | tuple[dict[str, Any], int]:
        task = db.get(AiAgentTask, int(task_id))
        if not task:
            return ({"error": "missing_task", "error_code": "missing_task"}, 404)
        msgs = (
            db.query(AiAgentMessage)
            .filter(AiAgentMessage.task_id == int(task_id))
            .order_by(AiAgentMessage.id.asc())
            .all()
        )
        facts = _latest_facts_payload(db, task_id)
        history = _history_from_messages(list(msgs))
        try:
            out = self._client.generate_draft(db, task, history, facts or None)
        except Exception as e:
            return ({"error": "generate_failed", "error_code": "generate_failed", "detail": str(e)}, 500)

        draft_body = str(out.get("draft_message") or "").strip()
        extracted = out.get("extracted_facts")
        if not isinstance(extracted, dict):
            extracted = {}

        (
            db.query(AiAgentMessage)
            .filter(
                AiAgentMessage.task_id == int(task_id),
                AiAgentMessage.direction.in_(("out", "outbound")),
                AiAgentMessage.status == "draft",
            )
            .delete(synchronize_session=False)
        )

        row = AiAgentMessage(
            task_id=int(task_id),
            direction="out",
            status="draft",
            body=draft_body,
            telegram_message_id=None,
            meta_json={"extracted_facts": extracted, "draft_meta": out.get("meta") or {}},
            created_at=datetime.utcnow(),
        )
        db.add(row)
        task.status = "waiting_admin_approval"
        task.updated_at = datetime.utcnow()
        task.last_activity_at = datetime.utcnow()
        ns = extracted.get("negotiation_stage")
        if isinstance(ns, str) and ns.strip():
            task.negotiation_stage = ns.strip()
        db.flush()
        return {"message": row, "extracted_facts": extracted}

    def approve_send(self, db: Session, task_id: int) -> dict[str, Any] | tuple[dict[str, Any], int]:
        task = db.get(AiAgentTask, int(task_id))
        if not task:
            return ({"error": "missing_task", "error_code": "missing_task"}, 404)
        d = latest_sendable_outbound_draft(db, task_id)
        if d is None:
            return {"ok": False, "error_code": "no_draft", "transient": False}
        text = (d.body or "").strip()
        if not text:
            return {"ok": False, "error_code": "empty_draft", "transient": False}
        from src.core.execution_guard import (
            ACTION_TELEGRAM_SEND,
            guard_blocked_ai_send,
            require_execution_allowed,
        )

        blocked = require_execution_allowed(
            ACTION_TELEGRAM_SEND,
            account_id=int(task.account_id),
        )
        if blocked is not None:
            payload = guard_blocked_ai_send(blocked)
            return {
                "ok": False,
                "error_code": payload.get("error_code"),
                "transient": False,
                "error_message": payload.get("error_message"),
                "execution_guard": payload.get("execution_guard"),
            }
        try:
            r = self._sender.send_message(
                int(task.account_id),
                str(task.target_username_or_id or ""),
                text,
            )
        except Exception as e:
            return {
                "ok": False,
                "error_code": "send_exception",
                "transient": True,
                "error_message": str(e),
            }
        if r.get("ok"):
            d.status = "sent"
            d.telegram_message_id = r.get("telegram_message_id")
            task.updated_at = datetime.utcnow()
            task.last_activity_at = datetime.utcnow()
            db.flush()
            return {
                "ok": True,
                "message": d,
                "telegram_message_id": r.get("telegram_message_id"),
                "transient": False,
            }
        return {
            "ok": False,
            "error_code": r.get("error_code"),
            "transient": bool(r.get("transient")),
        }

    def sync_inbound(
        self,
        db: Session,
        task_id: int,
        *,
        apply_autonomous_inbound_fetch_cooldown: bool = False,
    ) -> dict[str, Any] | tuple[dict[str, Any], int]:
        del apply_autonomous_inbound_fetch_cooldown  # optional hook; not enabled in minimal path
        task = db.get(AiAgentTask, int(task_id))
        if not task:
            return ({"error": "missing_task", "error_code": "missing_task"}, 404)
        try:
            res = self._sender.fetch_recent_messages(
                int(task.account_id),
                str(task.target_username_or_id or ""),
                30,
            )
        except Exception as e:
            return ({"error": "fetch_failed", "error_code": "fetch_failed", "detail": str(e)}, 500)
        if not res.get("ok"):
            code = str(res.get("error_code") or "fetch_error")
            if code == "forbidden_account":
                return {"inbound_fetch_disabled": True, "inbound_inserted": 0}
            return {
                "transient_telegram_busy": bool(res.get("transient")),
                "error_code": code,
                "inbound_inserted": 0,
            }
        inserted = 0
        for raw in res.get("messages") or []:
            if not isinstance(raw, dict):
                continue
            tid = _strip_tg_id(raw)
            if tid is None:
                continue
            exists = (
                db.query(AiAgentMessage)
                .filter(
                    AiAgentMessage.task_id == int(task_id),
                    AiAgentMessage.telegram_message_id == int(tid),
                )
                .first()
            )
            if exists:
                continue
            body = str(raw.get("text") or raw.get("body") or "")
            db.add(
                AiAgentMessage(
                    task_id=int(task_id),
                    direction="in",
                    status="stored",
                    body=body,
                    telegram_message_id=int(tid),
                    meta_json={},
                    created_at=datetime.utcnow(),
                )
            )
            inserted += 1
        task.last_activity_at = datetime.utcnow()
        db.flush()
        return {"inbound_inserted": inserted, "ok": True}
