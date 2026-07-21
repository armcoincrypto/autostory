"""P10.11 controlled scheduler runtime probe planning."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS, compute_account_operational_state
from src.core.models import Account
from src.core.scheduler_models import AccountTargetBinding, ChatTarget, JobStatus, ScheduledJob
from src.recovery.p9_83_governance_observability import PROTECTED_IDS, PURPOSE_HOLD_IDS

P10_11_JOB_MARKER = "__p10_11_runtime_validation_no_send__"
DEFAULT_VALIDATION_ACCOUNTS = (140, 150, 160, 170, 180)


@dataclass(frozen=True)
class SchedulerProbeConfig:
    accounts: tuple[int, ...] = DEFAULT_VALIDATION_ACCOUNTS
    mode: str = "self-test"
    max_jobs: int = 2
    apply: bool = False


def parse_account_ids(raw: str) -> tuple[int, ...]:
    ids = []
    for part in raw.split(","):
        p = part.strip()
        if p:
            ids.append(int(p))
    return tuple(sorted(set(ids)))


def _private_self_binding(db: Session, account_id: int) -> dict[str, Any] | None:
    row = (
        db.query(AccountTargetBinding, ChatTarget)
        .join(ChatTarget, AccountTargetBinding.target_id == ChatTarget.id)
        .filter(
            AccountTargetBinding.account_id == int(account_id),
            AccountTargetBinding.can_post == True,
            ChatTarget.chat_type == "private",
        )
        .order_by(ChatTarget.id.asc())
        .first()
    )
    if not row:
        return None
    binding, target = row
    title = (target.title or "").lower()
    if "saved messages" not in title and "self" not in title:
        return None
    return {
        "binding_id": int(binding.id),
        "target_id": int(target.id),
        "target_title": target.title,
        "chat_type": target.chat_type,
    }


def analyze_scheduler_probe_account(db: Session, account_id: int) -> dict[str, Any]:
    aid = int(account_id)
    blockers: list[str] = []
    acc = db.get(Account, aid)
    if acc is None:
        return {"account_id": aid, "safe": False, "blockers": ["account_not_found"]}
    if aid in PROTECTED_IDS:
        blockers.append("protected")
    if aid in PURPOSE_HOLD_IDS:
        blockers.append("held")
    if aid in CONTROLLER_ACCOUNT_IDS:
        blockers.append("controller")
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        blockers.append("ai_reserved")

    op = compute_account_operational_state(db, acc)
    if not op.get("scheduler_eligible"):
        blockers.append("scheduler_not_eligible")
    if op.get("readiness_status") != "READY":
        blockers.append("v1_not_ready")
    if op.get("resolver_code"):
        blockers.append(f"resolver_blocked:{op.get('resolver_code')}")

    active_jobs = (
        db.query(ScheduledJob)
        .filter(
            ScheduledJob.account_id == aid,
            ScheduledJob.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
        )
        .count()
    )
    if active_jobs:
        blockers.append(f"active_jobs_present:{active_jobs}")

    target = _private_self_binding(db, aid)
    if target is None:
        blockers.append("no_private_self_target_binding")

    return {
        "account_id": aid,
        "safe": not blockers,
        "blockers": blockers,
        "purpose": (acc.purpose or "").strip().lower() or None,
        "scheduler_eligible": op.get("scheduler_eligible"),
        "discovery_eligible": op.get("discovery_eligible"),
        "target": target,
        "active_jobs": int(active_jobs),
    }


def build_scheduler_probe_plan(db: Session, config: SchedulerProbeConfig) -> dict[str, Any]:
    analyses = [analyze_scheduler_probe_account(db, aid) for aid in config.accounts]
    clean = [a for a in analyses if a.get("safe")]
    planned_jobs = []
    for row in clean:
        for idx in range(max(0, min(int(config.max_jobs), 2))):
            planned_jobs.append(
                {
                    "account_id": int(row["account_id"]),
                    "target_id": int(row["target"]["target_id"]),
                    "type": "INFO",
                    "sequence": idx + 1,
                    "planned_transitions": ["PENDING", "RUNNING", "SKIPPED"],
                    "send_boundary": "not_crossed",
                    "marker": P10_11_JOB_MARKER,
                }
            )
    return {
        "track": "A",
        "mode": config.mode,
        "apply": config.apply,
        "accounts": list(config.accounts),
        "analyses": analyses,
        "clean_accounts": [int(a["account_id"]) for a in clean],
        "planned_jobs": planned_jobs,
        "blockers": [a for a in analyses if not a.get("safe")],
    }


def apply_scheduler_probe(db: Session, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("blockers"):
        return {**plan, "outcome": "SCHEDULER_PROBE_BLOCKED", "created_jobs": []}
    created = []
    now = datetime.utcnow()
    for item in plan.get("planned_jobs") or []:
        job = ScheduledJob(
            account_id=int(item["account_id"]),
            target_id=int(item["target_id"]),
            type=item["type"],
            run_at=now,
            status=JobStatus.SKIPPED.value,
            attempts=0,
            last_error=P10_11_JOB_MARKER,
            lease_owner=None,
            lease_until=None,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        created.append(
            {
                "job_id": int(job.id),
                "account_id": int(job.account_id),
                "target_id": int(job.target_id),
                "final_status": JobStatus.SKIPPED.value,
                "note": "terminal validation row only; scheduler executor and Telegram send not invoked",
            }
        )
    db.commit()
    return {**plan, "outcome": "SCHEDULER_PROBE_APPLIED_NO_SEND", "created_jobs": created}
