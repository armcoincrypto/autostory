"""
P9.32 — Global scheduler API mutation kill switch (default off).

When ``SCHEDULER_MUTATIONS_ENABLED`` is false, POST/PUT/DELETE routes that can
write DB state or reach Telegram are rejected with HTTP 423 before handlers run.

P9.40 — Optional scoped bypass: ``SCHEDULER_MUTATION_SCOPE=send_test_only`` with
``SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST`` allows **only** ``POST /api/v1/jobs/run-now``
for listed account IDs while global mutations remain disabled.

P9.66 — ``SCHEDULER_MUTATION_SCOPE=campaign_pilot_5`` additionally requires
``SCHEDULER_MUTATION_TARGET_ALLOWLIST`` (exactly five target IDs for five accounts).
Join, bulk, membership mutations remain blocked.

Wave D — Job-type mutation scope:
``SCHEDULER_PROMO_MUTATIONS_ENABLED`` / ``SCHEDULER_INFO_MUTATIONS_ENABLED``
further restrict PROMO/INFO creates even when the global switch is on.
Scheduled DM uses ``SCHEDULED_DM_ENABLED`` only (Messages API), not this module's
global switch.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from flask import Response, jsonify, request

from config.settings import settings

BLOCKED_STATUS = 423
BLOCKED_CODE = "scheduler_mutations_disabled"
BLOCKED_MESSAGE = (
    "Scheduler mutations are disabled (SCHEDULER_MUTATIONS_ENABLED=false). "
    "No DB writes, queue enqueue, or Telegram actions will run. "
    "Enable only in an explicit approved phase."
)
JOB_TYPE_BLOCKED_CODE = "scheduler_job_type_mutations_disabled"
SCOPED_SEND_TEST_SCOPE = "send_test_only"
SCOPED_CAMPAIGN_PILOT_SCOPE = "campaign_pilot_5"
MAX_CAMPAIGN_PILOT_ACCOUNTS = 5
RUN_NOW_PATH_SUFFIX = "/jobs/run-now"
BULK_APPLY_PATH_SUFFIX = "/schedule/bulk-apply"
JOIN_TARGETS_PATH_SUFFIX = "/scheduler/join-targets"
MEMBERSHIP_CHECK_PATH_SUFFIX = "/targets/membership-check"


def scheduler_mutations_enabled() -> bool:
    return bool(getattr(settings, "scheduler_mutations_enabled", False))


def _env_bool_flag(env_name: str, settings_attr: str) -> bool:
    """Fail-closed bool: explicit env wins; otherwise settings field (default false)."""
    env = (os.environ.get(env_name) or "").strip().lower()
    if env:
        return env in {"1", "true", "yes", "on"}
    return bool(getattr(settings, settings_attr, False))


def scheduler_promo_mutations_enabled() -> bool:
    """Product flag for PROMO job-type mutations (Wave D)."""
    return _env_bool_flag(
        "SCHEDULER_PROMO_MUTATIONS_ENABLED",
        "scheduler_promo_mutations_enabled",
    )


def scheduler_info_mutations_enabled() -> bool:
    """Product flag for INFO job-type mutations (Wave D)."""
    return _env_bool_flag(
        "SCHEDULER_INFO_MUTATIONS_ENABLED",
        "scheduler_info_mutations_enabled",
    )


def scheduler_promo_mutations_allowed() -> bool:
    """PROMO creates require global scheduler mutations AND the PROMO type flag."""
    return scheduler_mutations_enabled() and scheduler_promo_mutations_enabled()


def scheduler_info_mutations_allowed() -> bool:
    """INFO creates require global scheduler mutations AND the INFO type flag."""
    return scheduler_mutations_enabled() and scheduler_info_mutations_enabled()


def job_type_scheduler_mutation_allowed(job_type: str) -> bool:
    """Whether creating/enqueuing a scheduler job of this type is allowed."""
    jt = (job_type or "").strip().upper()
    if jt == "PROMO":
        return scheduler_promo_mutations_allowed()
    if jt == "INFO":
        return scheduler_info_mutations_allowed()
    if jt == "DM":
        # DM create is owned by Messages / SCHEDULED_DM_ENABLED — not /api/v1.
        from src.messaging.scheduled_dm_flags import scheduled_dm_create_allowed

        return scheduled_dm_create_allowed()
    return False


def job_type_mutation_block_payload(job_type: str) -> dict[str, Any]:
    jt = (job_type or "").strip().upper() or "UNKNOWN"
    return {
        "ok": False,
        "error": (
            f"Scheduler mutations for job type {jt} are disabled. "
            "PROMO requires SCHEDULER_MUTATIONS_ENABLED and SCHEDULER_PROMO_MUTATIONS_ENABLED; "
            "INFO requires SCHEDULER_MUTATIONS_ENABLED and SCHEDULER_INFO_MUTATIONS_ENABLED; "
            "DM requires SCHEDULED_DM_ENABLED only."
        ),
        "code": JOB_TYPE_BLOCKED_CODE,
        "error_code": JOB_TYPE_BLOCKED_CODE,
        "job_type": jt,
        "scheduler_mutations_enabled": scheduler_mutations_enabled(),
        "scheduler_promo_mutations_enabled": scheduler_promo_mutations_enabled(),
        "scheduler_info_mutations_enabled": scheduler_info_mutations_enabled(),
        "scheduler_promo_mutations_allowed": scheduler_promo_mutations_allowed(),
        "scheduler_info_mutations_allowed": scheduler_info_mutations_allowed(),
    }


def check_job_type_mutation_allowed(job_type: str) -> Optional[Response]:
    """Return 423 when this job type may not be created under Wave D type flags.

    When the global scheduler mutation switch is off, the blueprint ``before_request``
    guard (and scoped allowlists) already decided; do not double-block scoped pilots.
    When global is on, PROMO/INFO still require their per-type flags.
    """
    if not scheduler_mutations_enabled():
        return None
    if job_type_scheduler_mutation_allowed(job_type):
        return None
    return jsonify(job_type_mutation_block_payload(job_type)), BLOCKED_STATUS


def scheduler_mutation_account_allowlist_ids() -> frozenset[int]:
    raw = (getattr(settings, "scheduler_mutation_account_allowlist", None) or "").strip()
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


def scheduler_mutation_scope() -> str:
    return (getattr(settings, "scheduler_mutation_scope", None) or "").strip().lower()


def scheduler_mutation_target_allowlist_ids() -> frozenset[int]:
    raw = (getattr(settings, "scheduler_mutation_target_allowlist", None) or "").strip()
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


def scheduler_mutation_pair_allowlist_map() -> dict[int, int]:
    """account_id -> target_id for campaign_pilot_5 exact pairing."""
    raw = (getattr(settings, "scheduler_mutation_pair_allowlist", None) or "").strip()
    if not raw:
        return {}
    out: dict[int, int] = {}
    for part in raw.split(","):
        p = part.strip()
        if ":" not in p:
            continue
        a_s, t_s = p.split(":", 1)
        try:
            out[int(a_s.strip())] = int(t_s.strip())
        except ValueError:
            continue
    return out


def scoped_send_test_allowlist_active() -> bool:
    """True when scoped send-test bypass is configured (global mutations still off)."""
    if scheduler_mutations_enabled():
        return False
    if scheduler_mutation_scope() != SCOPED_SEND_TEST_SCOPE:
        return False
    return bool(scheduler_mutation_account_allowlist_ids())


def is_scoped_send_test_run_now_allowed(account_id: int) -> bool:
    return scoped_send_test_allowlist_active() and int(account_id) in scheduler_mutation_account_allowlist_ids()


def scoped_campaign_pilot_allowlist_active() -> bool:
    if scheduler_mutations_enabled():
        return False
    if scheduler_mutation_scope() != SCOPED_CAMPAIGN_PILOT_SCOPE:
        return False
    aids = scheduler_mutation_account_allowlist_ids()
    tids = scheduler_mutation_target_allowlist_ids()
    return (
        len(aids) == MAX_CAMPAIGN_PILOT_ACCOUNTS
        and len(tids) == MAX_CAMPAIGN_PILOT_ACCOUNTS
        and len(aids) > 0
    )


def is_scoped_campaign_pilot_run_now_allowed(account_id: int, target_id: int) -> bool:
    if not scoped_campaign_pilot_allowlist_active():
        return False
    aid = int(account_id)
    tid = int(target_id)
    if aid not in scheduler_mutation_account_allowlist_ids():
        return False
    if tid not in scheduler_mutation_target_allowlist_ids():
        return False
    pairs = scheduler_mutation_pair_allowlist_map()
    if pairs:
        return pairs.get(aid) == tid
    return True


def _run_now_target_id_from_body() -> Optional[int]:
    body = _json_body()
    raw = body.get("target_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_join_targets_path() -> bool:
    return (request.path or "").rstrip("/").endswith(JOIN_TARGETS_PATH_SUFFIX)


def _is_bulk_apply_path() -> bool:
    return (request.path or "").rstrip("/").endswith(BULK_APPLY_PATH_SUFFIX)


def _is_membership_check_path() -> bool:
    return (request.path or "").rstrip("/").endswith(MEMBERSHIP_CHECK_PATH_SUFFIX)


def scheduler_mutation_block_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": BLOCKED_MESSAGE,
        "code": BLOCKED_CODE,
        "scheduler_mutations_enabled": False,
        "path": request.path,
        "method": request.method,
    }
    if scoped_send_test_allowlist_active():
        payload["scoped_send_test_allowlist_active"] = True
        payload["scoped_allowlist_account_ids"] = sorted(scheduler_mutation_account_allowlist_ids())
        payload["scoped_mutation_scope"] = scheduler_mutation_scope()
    if scoped_campaign_pilot_allowlist_active():
        payload["scoped_campaign_pilot_active"] = True
        payload["scoped_allowlist_account_ids"] = sorted(scheduler_mutation_account_allowlist_ids())
        payload["scoped_allowlist_target_ids"] = sorted(scheduler_mutation_target_allowlist_ids())
        pairs = scheduler_mutation_pair_allowlist_map()
        if pairs:
            payload["scoped_allowlist_pairs"] = {str(k): v for k, v in sorted(pairs.items())}
        payload["scoped_mutation_scope"] = scheduler_mutation_scope()
    return payload


def _json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _dry_run_requested() -> bool:
    body = _json_body()
    if "dry_run" not in body:
        return False
    return bool(body.get("dry_run"))


def _is_dry_run_exempt_path() -> bool:
    """Paths allowed when mutations disabled and request is read-only / dry-run."""
    path = (request.path or "").rstrip("/")
    if path.endswith("/targets/dedupe/dry-run"):
        return True
    if "/scheduler/orphan-profile/" in path and request.method == "POST":
        return _dry_run_requested()
    if path.endswith("/targets/dedupe/apply") and request.method == "POST":
        body = _json_body()
        if "dry_run" not in body:
            return True
        return bool(body.get("dry_run"))
    return False


def _is_run_now_path() -> bool:
    return (request.path or "").rstrip("/").endswith(RUN_NOW_PATH_SUFFIX)


def _run_now_account_id_from_body() -> Optional[int]:
    body = _json_body()
    raw = body.get("account_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def check_scheduler_mutation_allowed() -> Optional[Response]:
    """
    Return a Flask response to short-circuit, or None if the request may proceed.
    """
    if scheduler_mutations_enabled():
        return None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if _is_dry_run_exempt_path():
        return None
    if _is_run_now_path() and request.method == "POST":
        aid = _run_now_account_id_from_body()
        tid = _run_now_target_id_from_body()
        if aid is not None and is_scoped_send_test_run_now_allowed(aid):
            return None
        if (
            aid is not None
            and tid is not None
            and is_scoped_campaign_pilot_run_now_allowed(aid, tid)
        ):
            return None
    if scoped_campaign_pilot_allowlist_active() and request.method == "POST":
        if _is_join_targets_path() or _is_bulk_apply_path() or _is_membership_check_path():
            return jsonify(scheduler_mutation_block_payload()), BLOCKED_STATUS
    return jsonify(scheduler_mutation_block_payload()), BLOCKED_STATUS


# Catalog for P9.32 smoke / docs (method, path suffix, category, telegram_boundary).
SCHEDULER_MUTATION_ROUTE_CATALOG: tuple[dict[str, str], ...] = (
    {"method": "POST", "path": "/scheduler/join-targets", "category": "join", "telegram_boundary": "join_target_for_account"},
    {"method": "POST", "path": "/scheduler/orphan-profile/<account_id>", "category": "cleanup", "telegram_boundary": "db_delete_only"},
    {"method": "POST", "path": "/accounts/<account_id>/telegram-joined-groups/bind", "category": "bind", "telegram_boundary": "db_bindings_only"},
    {"method": "POST", "path": "/targets", "category": "targets", "telegram_boundary": "db_write"},
    {"method": "POST", "path": "/targets/<target_id>/clear-cached-id", "category": "targets", "telegram_boundary": "db_write"},
    {"method": "POST", "path": "/targets/dedupe/dry-run", "category": "dedupe", "telegram_boundary": "read_only_plan"},
    {"method": "POST", "path": "/targets/dedupe/apply", "category": "dedupe", "telegram_boundary": "db_write_if_not_dry_run"},
    {"method": "POST", "path": "/targets/membership-check", "category": "membership", "telegram_boundary": "check_targets_membership_sequential"},
    {"method": "DELETE", "path": "/targets/<target_id>", "category": "targets", "telegram_boundary": "db_cascade"},
    {"method": "POST", "path": "/targets/<target_id>/verify", "category": "targets", "telegram_boundary": "telethon_resolve"},
    {"method": "POST", "path": "/targets/<target_id>/repair", "category": "targets", "telegram_boundary": "telethon_resolve"},
    {"method": "POST", "path": "/bindings", "category": "bindings", "telegram_boundary": "db_write"},
    {"method": "PUT", "path": "/bindings/<binding_id>", "category": "bindings", "telegram_boundary": "db_write"},
    {"method": "DELETE", "path": "/bindings/<binding_id>", "category": "bindings", "telegram_boundary": "db_write"},
    {"method": "POST", "path": "/templates", "category": "templates", "telegram_boundary": "db_write"},
    {"method": "PUT", "path": "/templates/<template_id>", "category": "templates", "telegram_boundary": "db_write"},
    {"method": "DELETE", "path": "/templates/<template_id>", "category": "templates", "telegram_boundary": "db_write"},
    {"method": "POST", "path": "/templates/preview", "category": "templates", "telegram_boundary": "render_only"},
    {"method": "PUT", "path": "/schedule/profile/<account_id>", "category": "campaign_save", "telegram_boundary": "db_write"},
    {"method": "POST", "path": "/schedule/rules/<account_id>", "category": "campaign_save", "telegram_boundary": "db_write"},
    {"method": "DELETE", "path": "/schedule/rules/<account_id>/<rule_id>", "category": "campaign_save", "telegram_boundary": "db_write"},
    {"method": "POST", "path": "/schedule/bulk-apply", "category": "bulk", "telegram_boundary": "db_write_multi_account"},
    {"method": "POST", "path": "/jobs/run-now", "category": "send_test", "telegram_boundary": "ScheduledJob_enqueue_then_executor_send"},
)
