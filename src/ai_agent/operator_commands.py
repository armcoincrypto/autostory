"""
Telegram operator commands for AI Agent (parsing + DB actions).

Uses AiAgentService, allowlist, and target dedupe. No Telegram sessions here.
Never logs goal text or secrets.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from config.settings import settings
from src.ai_agent.account_allowlist import (
    account_id_permitted_for_ai_agent_tasks,
    resolve_ai_agent_task_permitted_account_ids,
)
from src.ai_agent.serializers import (
    serialize_message,
    serialize_profit_facts_for_ui,
    serialize_task_full,
)
from src.ai_agent.service import AiAgentService, _latest_facts_payload
from src.ai_agent.task_target_dedupe import (
    find_active_task_id_for_normalized_target,
    normalize_ai_target,
)
from src.core.ai_agent_models import AiAgentMessage, AiAgentTask
from src.core.models import Account

logger = structlog.get_logger(__name__)

ACCOUNT_PERSONAS: dict[int, str] = {
    110: "professional",
    113: "strict",
    131: "friendly",
}

_NOT_AUTH = "Not authorized."


def parse_operator_telegram_ids() -> frozenset[int]:
    raw = (settings.ai_agent_telegram_operator_ids or "").strip()
    if not raw:
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except ValueError:
            continue
    return frozenset(out)


def is_operator_telegram_user(user_id: int) -> bool:
    return int(user_id) in parse_operator_telegram_ids()


def default_ai_account_id() -> int:
    return int(settings.ai_agent_default_account_id or 110)


def _tone_for_account(account_id: int) -> str:
    return ACCOUNT_PERSONAS.get(int(account_id), "professional")


def _short_task_status_lines(db: Session, task: AiAgentTask) -> list[str]:
    facts = _latest_facts_payload(db, int(task.id))
    fd = facts if isinstance(facts, dict) else {}
    pf = serialize_profit_facts_for_ui(fd)
    ser = serialize_task_full(task, facts=fd, profit_facts=pf)
    lines = [
        f"Task #{task.id}",
        f"Status: {ser.get('ui_status_label') or task.status}",
        f"Target: {task.target_username_or_id}",
        f"Account: #{task.account_id}",
    ]
    return lines


def format_ai_help() -> str:
    return (
        "**AI Agent (desk)**\n\n"
        "**Commands**\n"
        "`/ai_help` — this message\n"
        "`/ai_accounts` — AI lines #110 / #113 / #131\n"
        "`/ai_new @target goal…` — new task (default account)\n"
        "`/ai_new_110` / `_113` / `_131` — pick account\n"
        "`/ai @target goal…` — same as `/ai_new`\n"
        "`/ai_status ID` — status + facts + recent messages\n"
        "`/ai_summary ID` — briefing (side, rate, …)\n"
        "`/ai_pause ID` · `/ai_resume ID`\n"
        "`/ai_send ID` — send latest draft\n"
        "`/ai_run ID` — sync inbox + one draft (no auto-send)\n\n"
        "**Natural (Kathleen)**\n"
        "`Kathleen, write @user and …`\n"
        "`Kathleen ask @user about …`\n"
        "`Kathleen status 33` · `Kathleen summary 33`\n"
    )


def format_ai_accounts_list(db: Session) -> str:
    permitted = sorted(resolve_ai_agent_task_permitted_account_ids(db))
    lines = ["**AI Agent accounts**", ""]
    for aid in (110, 113, 131):
        if aid not in permitted:
            continue
        label = ACCOUNT_PERSONAS.get(aid, "professional")
        row = db.query(Account).filter(Account.id == aid).first()
        uname = f"@{row.username}" if row and row.username else "(no username)"
        lines.append(f"#{aid} — {label} — {uname}")
    if len(lines) <= 2:
        lines.append("_No dedicated AI accounts resolved (check allowlist)._")
    return "\n".join(lines)


def _parse_target_and_goal(rest: str) -> tuple[Optional[str], Optional[str]]:
    s = (rest or "").strip()
    if not s.startswith("@"):
        return None, None
    sp = s.split(None, 1)
    if len(sp) < 2 or not sp[1].strip():
        return None, None
    return sp[0].strip(), sp[1].strip()


def parse_natural_kathleen(text: str) -> Optional[tuple[str, tuple[Any, ...]]]:
    raw = (text or "").strip()
    if not re.match(r"(?is)^kathleen\b", raw):
        return None
    tail = re.sub(r"(?is)^kathleen[,:]?\s+", "", raw).strip()
    if not tail:
        return None
    m = re.match(r"(?i)^status\s+#?(\d+)\s*$", tail)
    if m:
        return ("ai_status", (int(m.group(1)),))
    m = re.match(r"(?i)^summary\s+#?(\d+)\s*$", tail)
    if m:
        return ("ai_summary", (int(m.group(1)),))
    m = re.match(r"(?i)^(pause|resume|send|run)\s+#?(\d+)\s*$", tail)
    if m:
        verb = m.group(1).lower()
        tid = int(m.group(2))
        return (f"ai_{verb}", (tid,))
    m = re.search(r"@\S+", tail)
    if not m:
        return None
    target = m.group(0)
    pos = tail.find(target) + len(target)
    goal = tail[pos:].strip()
    if goal.lower().startswith("and "):
        goal = goal[4:].strip()
    goal = re.sub(r"(?i)^(write|ask|get)\s+", "", goal).strip()
    if not goal:
        return None
    return ("ai_new", (default_ai_account_id(), target, goal))


def parse_slash_command(text: str) -> Optional[tuple[str, tuple[Any, ...]]]:
    line = (text or "").strip()
    if not line.startswith("/"):
        return None
    low = line.lower()
    if low == "/ai_help":
        return ("ai_help", ())
    if low == "/ai_accounts":
        return ("ai_accounts", ())
    m = re.match(r"(?i)^/ai_new_(110|113|131)\s+(.+)$", line)
    if m:
        aid = int(m.group(1))
        tgt, goal = _parse_target_and_goal(m.group(2))
        if not tgt or not goal:
            return ("ai_new_bad", ())
        return ("ai_new", (aid, tgt, goal))
    m = re.match(r"(?i)^/ai_new\s+(.+)$", line)
    if m:
        tgt, goal = _parse_target_and_goal(m.group(1))
        if not tgt or not goal:
            return ("ai_new_bad", ())
        return ("ai_new", (default_ai_account_id(), tgt, goal))
    m = re.match(r"(?i)^/ai\s+(@\S+)\s+(.+)$", line)
    if m:
        return ("ai_new", (default_ai_account_id(), m.group(1).strip(), m.group(2).strip()))
    m = re.match(r"(?i)^/ai_status\s+(\d+)\s*$", line)
    if m:
        return ("ai_status", (int(m.group(1)),))
    m = re.match(r"(?i)^/ai_summary\s+(\d+)\s*$", line)
    if m:
        return ("ai_summary", (int(m.group(1)),))
    for verb in ("pause", "resume", "send", "run"):
        m = re.match(rf"(?i)^/ai_{verb}\s+(\d+)\s*$", line)
        if m:
            return (f"ai_{verb}", (int(m.group(1)),))
    return None


def _get_task(db: Session, task_id: int) -> Optional[AiAgentTask]:
    return db.get(AiAgentTask, int(task_id))


def _messages_for_serialize(db: Session, task_id: int, limit: int = 25) -> list[dict[str, Any]]:
    rows = (
        db.query(AiAgentMessage)
        .filter(AiAgentMessage.task_id == int(task_id))
        .order_by(AiAgentMessage.id.desc())
        .limit(limit)
        .all()
    )
    return [serialize_message(m) for m in reversed(rows)]


def format_ai_summary_text(db: Session, task_id: int) -> str:
    t = _get_task(db, task_id)
    if not t:
        return f"Task #{task_id} not found."
    facts = _latest_facts_payload(db, int(task_id))
    fd = facts if isinstance(facts, dict) else {}
    pf = serialize_profit_facts_for_ui(fd)
    ser = serialize_task_full(t, facts=fd, profit_facts=pf, messages=_messages_for_serialize(db, task_id))
    rows = ser.get("ui_deal_summary_rows") or []
    lines = [f"**Task #{task_id}**", ""]
    for r in rows:
        if isinstance(r, dict):
            lines.append(f"**{r.get('label', '')}:** {r.get('value', '')}")
    ra = ser.get("ui_recommended_action")
    if ra:
        lines.append("")
        lines.append(f"**Recommended:** {ra}")
    return "\n".join(lines)


def format_ai_status_text(db: Session, task_id: int) -> str:
    t = _get_task(db, task_id)
    if not t:
        return f"Task #{task_id} not found."
    facts = _latest_facts_payload(db, int(task_id))
    fd = facts if isinstance(facts, dict) else {}
    pf = serialize_profit_facts_for_ui(fd)
    msgs = _messages_for_serialize(db, task_id, limit=12)
    ser = serialize_task_full(t, facts=fd, profit_facts=pf, messages=msgs)
    lines = [
        f"**Task #{task_id}**",
        f"Status: {ser.get('ui_status_label') or t.status}",
        f"Target: {t.target_username_or_id}",
        f"Next: {ser.get('ui_next_action') or '—'}",
        "",
        "**Latest facts**",
    ]
    if fd:
        for k, v in list(fd.items())[:18]:
            if v is None or v == "":
                continue
            lines.append(f"• `{k}`: {v}")
    else:
        lines.append("_none yet_")
    lines.extend(["", "**Recent messages**"])
    if msgs:
        for m in msgs[-8:]:
            d = (m.get("direction") or "")[:1].upper()
            body = (m.get("body") or "")[:200]
            lines.append(f"[{d}] {body}")
    else:
        lines.append("_none_")
    return "\n".join(lines)


def build_operator_ready_notification_text(task_id: int, headline: str, extra: str = "") -> str:
    body = f"{headline}\nOpen dashboard: AI Agent → Task #{task_id}"
    if extra.strip():
        body += f"\n\n{extra.strip()}"
    return body


def _humanize_send(res: dict[str, Any]) -> str:
    if res.get("ok"):
        return "Sent."
    code = str(res.get("error_code") or "send_failed")
    if res.get("transient"):
        return f"Telegram is busy ({code}). Try /ai_send again shortly. Task is unchanged in the dashboard."
    return f"Send failed ({code}). Check dashboard."


def create_ai_task(
    db: Session,
    *,
    account_id: int,
    target: str,
    goal_text: str,
    tone: Optional[str] = None,
) -> tuple[Optional[AiAgentTask], str]:
    norm = normalize_ai_target(target)
    if not norm:
        return None, "Invalid target. Use @username or numeric Telegram id."

    if not account_id_permitted_for_ai_agent_tasks(db, int(account_id)):
        return None, f"Account #{account_id} is not permitted for AI Agent. Use /ai_accounts."

    if db.get(Account, int(account_id)) is None:
        return None, f"Account #{account_id} does not exist."

    existing_id = find_active_task_id_for_normalized_target(db, norm)
    if existing_id is not None:
        other = db.get(AiAgentTask, int(existing_id))
        msg = f"Active task already exists: #{existing_id}"
        if other:
            msg += "\n" + "\n".join(_short_task_status_lines(db, other))
        return None, msg

    now = datetime.utcnow()
    t = AiAgentTask(
        account_id=int(account_id),
        target_username_or_id=str(target).strip(),
        goal_text=str(goal_text).strip(),
        language="auto",
        tone=(tone or _tone_for_account(account_id)).strip() or "professional",
        max_messages=10,
        status="draft",
        negotiation_stage="opening",
        auto_mode="autonomous",
        auto_delay_sec=20,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    logger.info("ai_agent_task_created_via_operator", task_id=int(t.id), account_id=int(account_id))
    return t, ""


def bootstrap_new_task_telegram(
    db: Session,
    task_id: int,
    svc: AiAgentService,
    *,
    send_first_message: bool,
) -> str:
    """Sync → generate → optional send. Caller should commit after success paths."""
    tid = int(task_id)
    sync = svc.sync_inbound(db, tid)
    if isinstance(sync, tuple):
        return "Task created but inbox sync failed (transient or error). Use `/ai_run {tid}` later.".replace(
            "{tid}", str(tid)
        )
    gen = svc.generate_draft(db, tid)
    if isinstance(gen, tuple):
        return "Task created but draft generation failed. Check dashboard logs."
    if not send_first_message:
        db.commit()
        return f"Task #{tid} ready. `/ai_send {tid}` to post the draft, or use the web dashboard."
    send = svc.approve_send(db, tid)
    if not send.get("ok"):
        db.commit()
        return f"Task #{tid} created; draft saved. {_humanize_send(send)}"
    db.commit()
    return f"Task #{tid} created and first message sent."


def run_ai_task_step(db: Session, task_id: int, svc: AiAgentService) -> str:
    tid = int(task_id)
    sync = svc.sync_inbound(db, tid)
    if isinstance(sync, tuple):
        return "Sync failed."
    gen = svc.generate_draft(db, tid)
    if isinstance(gen, tuple):
        return "Generate failed."
    db.commit()
    return f"Task #{tid}: synced + draft updated."


def execute_parsed_command(
    db: Session,
    operator_id: int,
    cmd: str,
    args: tuple[Any, ...],
    svc: AiAgentService,
) -> str:
    if not is_operator_telegram_user(operator_id):
        return _NOT_AUTH

    if cmd == "ai_help":
        return format_ai_help()
    if cmd == "ai_accounts":
        return format_ai_accounts_list(db)
    if cmd == "ai_new_bad":
        return "Usage: `/ai_new @target your goal text…` (goal required)."

    if cmd == "ai_new":
        aid, target, goal = int(args[0]), str(args[1]), str(args[2])
        t, err = create_ai_task(db, account_id=aid, target=target, goal_text=goal, tone=_tone_for_account(aid))
        if err:
            return err
        assert t is not None
        return bootstrap_new_task_telegram(db, int(t.id), svc, send_first_message=True)

    if cmd == "ai_status":
        return format_ai_status_text(db, int(args[0]))
    if cmd == "ai_summary":
        return format_ai_summary_text(db, int(args[0]))

    tid = int(args[0])
    t = _get_task(db, tid)
    if not t:
        return f"Task #{tid} not found."

    if cmd == "ai_pause":
        t.status = "paused"
        t.updated_at = datetime.utcnow()
        db.commit()
        return f"Task #{tid} paused."

    if cmd == "ai_resume":
        from src.ai_agent.service import latest_sendable_outbound_draft

        if latest_sendable_outbound_draft(db, tid) is not None:
            t.status = "waiting_admin_approval"
        else:
            t.status = "draft"
        t.updated_at = datetime.utcnow()
        db.commit()
        return f"Task #{tid} resumed."

    if cmd == "ai_send":
        r = svc.approve_send(db, tid)
        db.commit()
        return _humanize_send(r) if r.get("ok") else _humanize_send(r)

    if cmd == "ai_run":
        return run_ai_task_step(db, tid, svc)

    return "Unknown command."


def handle_operator_incoming_text(
    db: Session,
    operator_telegram_user_id: int,
    text: str,
    *,
    service: Optional[AiAgentService] = None,
) -> Optional[str]:
    """
    If text is an AI operator command, return a reply string.
    Returns None when this module does not handle the line (caller may continue).
    """
    if not parse_operator_telegram_ids():
        return None

    parsed = parse_slash_command(text)
    if parsed is None:
        parsed = parse_natural_kathleen(text)
    if parsed is None:
        return None

    cmd, args = parsed
    svc = service or AiAgentService()
    return execute_parsed_command(db, operator_telegram_user_id, cmd, args, svc)


def audit_operator_push_sent(db: Session, task_id: int, *, event: str) -> None:
    from src.ai_agent.service import _append_audit

    _append_audit(
        db,
        action="operator_telegram_push_sent",
        task_id=int(task_id),
        account_id=None,
        detail_json={"event": str(event)},
    )
    db.commit()


def should_send_operator_push(db: Session, task_id: int, event: str) -> bool:
    from src.core.ai_agent_models import AiAgentAudit

    exists = (
        db.query(AiAgentAudit)
        .filter(
            AiAgentAudit.task_id == int(task_id),
            AiAgentAudit.action == "operator_telegram_push_sent",
        )
        .first()
    )
    if exists is None:
        return True
    d = exists.detail_json if isinstance(exists.detail_json, dict) else {}
    return str(d.get("event") or "") != str(event)
