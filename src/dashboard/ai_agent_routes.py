"""AI Agent dashboard JSON API (operator actions + task detail)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import Blueprint, jsonify, request

from src.ai_agent.account_allowlist import (
    account_id_permitted_for_ai_agent_tasks,
    resolve_ai_agent_task_permitted_account_ids,
)
from src.ai_agent.public_errors import enrich_error_payload
from src.ai_agent.serializers import (
    serialize_audit_row,
    serialize_message,
    serialize_profit_facts_for_ui,
    serialize_task_full,
)
from src.ai_agent.service import (
    AiAgentService,
    _latest_facts_payload,
    latest_sendable_outbound_draft,
)
from src.ai_agent.task_target_dedupe import (
    find_active_task_id_for_normalized_target,
    normalize_ai_target,
)
from src.core.ai_agent_models import AiAgentAudit, AiAgentMessage, AiAgentTask
from src.core.database import get_db_context
from src.core.models import Account

from .auth_access import dashboard_api_authorized

ai_agent_api = Blueprint("ai_agent_api", __name__, url_prefix="/api/v1/ai-agent")


@ai_agent_api.before_request
def _require_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return None


def _draft_snapshot(db: Any, task_id: int) -> dict[str, Any] | None:
    m = latest_sendable_outbound_draft(db, int(task_id))
    if m is None:
        return None
    meta = m.meta_json if isinstance(m.meta_json, dict) else {}
    dm = meta.get("draft_meta") if isinstance(meta, dict) else {}
    prov = dm.get("provider") if isinstance(dm, dict) else None
    out = serialize_message(m)
    out["provider"] = prov
    return out


def _task_runtime_flags(db: Any, t: AiAgentTask) -> dict[str, Any]:
    return {
        "account_ai_permitted": account_id_permitted_for_ai_agent_tasks(db, int(t.account_id)),
    }


def _task_detail_payload(db: Any, t: AiAgentTask, include: set[str]) -> dict[str, Any]:
    tid = int(t.id)
    facts = _latest_facts_payload(db, tid)
    facts_d = facts if isinstance(facts, dict) else {}
    pf = serialize_profit_facts_for_ui(facts_d)
    msgs: list[dict[str, Any]] = []
    if "messages" in include:
        rows = (
            db.query(AiAgentMessage)
            .filter(AiAgentMessage.task_id == tid)
            .order_by(AiAgentMessage.id.desc())
            .limit(120)
            .all()
        )
        msgs = [serialize_message(m) for m in reversed(rows)]
    payload: dict[str, Any] = serialize_task_full(
        t,
        runtime=_task_runtime_flags(db, t),
        facts=facts_d,
        profit_facts=pf,
        messages=msgs or None,
    )
    payload["extracted_facts"] = facts_d
    payload["profit_facts"] = pf
    if "messages" in include:
        payload["messages"] = msgs
    if "audits" in include:
        rows = (
            db.query(AiAgentAudit)
            .filter(AiAgentAudit.task_id == tid)
            .order_by(AiAgentAudit.id.desc())
            .limit(150)
            .all()
        )
        payload["audits"] = [serialize_audit_row(a) for a in reversed(rows)]
    payload["draft"] = _draft_snapshot(db, tid)
    return payload


@ai_agent_api.route("/accounts", methods=["GET"])
def list_ai_agent_accounts():
    """Dedicated AI Agent Telegram lines only (never campaign/story pool)."""
    with get_db_context() as db:
        permitted = resolve_ai_agent_task_permitted_account_ids(db)
        if not permitted:
            return jsonify({"accounts": []})
        rows = (
            db.query(Account)
            .filter(Account.id.in_(tuple(sorted(permitted))))
            .order_by(Account.id.asc())
            .all()
        )
        out: list[dict[str, Any]] = []
        for a in rows:
            out.append(
                {
                    "id": a.id,
                    "phone_number": a.phone_number or "",
                    "username": a.username or "",
                    "purpose": a.purpose or "",
                }
            )
        return jsonify({"accounts": out})


@ai_agent_api.route("/tasks", methods=["GET"])
def list_tasks():
    with get_db_context() as db:
        permitted = resolve_ai_agent_task_permitted_account_ids(db)
        rows = (
            db.query(AiAgentTask)
            .filter(AiAgentTask.account_id.in_(tuple(sorted(permitted))))
            .order_by(AiAgentTask.id.desc())
            .limit(200)
            .all()
        )
        out: list[dict[str, Any]] = []
        for t in rows:
            facts = _latest_facts_payload(db, int(t.id))
            facts_d = facts if isinstance(facts, dict) else {}
            pf = serialize_profit_facts_for_ui(facts_d)
            entry = serialize_task_full(
                t, runtime=_task_runtime_flags(db, t), facts=facts_d, profit_facts=pf
            )
            entry["profit_facts"] = pf
            out.append(entry)
        return jsonify({"tasks": out})


@ai_agent_api.route("/tasks", methods=["POST"])
def create_task():
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_account_id"}), 400
    target = str(data.get("target") or "").strip()
    goal = str(data.get("goal") or "").strip()
    if not target or not goal:
        return jsonify({"error": "target_and_goal_required"}), 400
    language = str(data.get("language") or "auto").strip() or "auto"
    tone = str(data.get("tone") or "professional").strip() or "professional"
    try:
        max_messages = int(data.get("max_messages") or 10)
    except (TypeError, ValueError):
        max_messages = 10
    max_messages = max(1, min(max_messages, 500))
    auto_mode = str(data.get("auto_mode") or "autonomous").strip().lower()
    if auto_mode not in ("off", "supervised", "autonomous"):
        auto_mode = "autonomous"

    with get_db_context() as db:
        if db.get(Account, account_id) is None:
            return jsonify({"error": "account_not_found"}), 400
        if not account_id_permitted_for_ai_agent_tasks(db, account_id):
            return jsonify(
                enrich_error_payload(
                    {
                        "ok": False,
                        "error": "account_not_allowed_for_ai_agent",
                        "human_message": (
                            "This account is not reserved for AI Agent. "
                            "Use #110, #113, or #131."
                        ),
                    }
                )
            ), 403
        norm = normalize_ai_target(target)
        existing_id = find_active_task_id_for_normalized_target(db, norm)
        if existing_id is not None:
            return jsonify(
                enrich_error_payload(
                    {
                        "ok": False,
                        "error": "active_task_exists_for_target",
                        "existing_task_id": int(existing_id),
                        "human_message": (
                            "An active task already exists for this target. "
                            "Open it or pause it first."
                        ),
                    }
                )
            ), 409
        now = datetime.utcnow()
        t = AiAgentTask(
            account_id=account_id,
            target_username_or_id=target,
            goal_text=goal,
            language=language,
            tone=tone,
            max_messages=max_messages,
            status="draft",
            negotiation_stage="opening",
            auto_mode=auto_mode,
            auto_delay_sec=20,
            last_activity_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return jsonify(serialize_task_full(t, facts={}, profit_facts={})), 201


@ai_agent_api.route("/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id: int):
    inc_raw = (request.args.get("include") or "").strip()
    include = {x.strip().lower() for x in inc_raw.split(",") if x.strip()}
    with get_db_context() as db:
        t = db.get(AiAgentTask, int(task_id))
        if not t:
            return jsonify({"error": "not_found"}), 404
        return jsonify(_task_detail_payload(db, t, include))


@ai_agent_api.route("/tasks/<int:task_id>/generate-draft", methods=["POST"])
def generate_draft_route(task_id: int):
    with get_db_context() as db:
        svc = AiAgentService()
        res = svc.generate_draft(db, int(task_id))
        if isinstance(res, tuple):
            db.rollback()
            return jsonify(res[0]), int(res[1])
        db.commit()
        t = db.get(AiAgentTask, int(task_id))
        assert t is not None
        facts = _latest_facts_payload(db, int(task_id))
        facts_d = facts if isinstance(facts, dict) else {}
        pf = serialize_profit_facts_for_ui(facts_d)
        return jsonify(
            {
                "ok": True,
                "task": serialize_task_full(t, facts=facts_d, profit_facts=pf),
                "draft": _draft_snapshot(db, int(task_id)),
                "extracted_facts": facts_d,
                "profit_facts": pf,
            }
        )


@ai_agent_api.route("/tasks/<int:task_id>/send", methods=["POST"])
def send_draft_route(task_id: int):
    with get_db_context() as db:
        svc = AiAgentService()
        res = svc.approve_send(db, int(task_id))
        if isinstance(res, tuple):
            db.rollback()
            return jsonify(res[0]), int(res[1])
        db.commit()
        t = db.get(AiAgentTask, int(task_id))
        assert t is not None
        facts = _latest_facts_payload(db, int(task_id))
        facts_d = facts if isinstance(facts, dict) else {}
        pf = serialize_profit_facts_for_ui(facts_d)
        return jsonify(
            {
                "ok": bool(res.get("ok")),
                "task": serialize_task_full(t, facts=facts_d, profit_facts=pf),
                "telegram_message_id": res.get("telegram_message_id"),
                "error_code": res.get("error_code"),
                "transient": res.get("transient"),
            }
        )


@ai_agent_api.route("/tasks/<int:task_id>/sync-inbound", methods=["POST"])
def sync_inbound_route(task_id: int):
    with get_db_context() as db:
        svc = AiAgentService()
        res = svc.sync_inbound(db, int(task_id))
        if isinstance(res, tuple):
            db.rollback()
            return jsonify(res[0]), int(res[1])
        db.commit()
        return jsonify(res)


@ai_agent_api.route("/tasks/<int:task_id>/pause", methods=["POST"])
def pause_task(task_id: int):
    with get_db_context() as db:
        t = db.get(AiAgentTask, int(task_id))
        if not t:
            return jsonify({"error": "not_found"}), 404
        t.status = "paused"
        t.updated_at = datetime.utcnow()
        db.commit()
        facts = _latest_facts_payload(db, int(task_id))
        facts_d = facts if isinstance(facts, dict) else {}
        pf = serialize_profit_facts_for_ui(facts_d)
        return jsonify({"ok": True, "task": serialize_task_full(t, facts=facts_d, profit_facts=pf)})


@ai_agent_api.route("/tasks/<int:task_id>/resume", methods=["POST"])
def resume_task(task_id: int):
    with get_db_context() as db:
        t = db.get(AiAgentTask, int(task_id))
        if not t:
            return jsonify({"error": "not_found"}), 404
        if latest_sendable_outbound_draft(db, int(task_id)) is not None:
            t.status = "waiting_admin_approval"
        else:
            t.status = "draft"
        t.updated_at = datetime.utcnow()
        db.commit()
        facts = _latest_facts_payload(db, int(task_id))
        facts_d = facts if isinstance(facts, dict) else {}
        pf = serialize_profit_facts_for_ui(facts_d)
        return jsonify({"ok": True, "task": serialize_task_full(t, facts=facts_d, profit_facts=pf)})


@ai_agent_api.route("/tasks/<int:task_id>/complete", methods=["POST"])
def complete_task(task_id: int):
    with get_db_context() as db:
        t = db.get(AiAgentTask, int(task_id))
        if not t:
            return jsonify({"error": "not_found"}), 404
        t.status = "completed"
        t.negotiation_stage = "completed"
        t.updated_at = datetime.utcnow()
        db.commit()
        facts = _latest_facts_payload(db, int(task_id))
        facts_d = facts if isinstance(facts, dict) else {}
        pf = serialize_profit_facts_for_ui(facts_d)
        return jsonify({"ok": True, "task": serialize_task_full(t, facts=facts_d, profit_facts=pf)})
