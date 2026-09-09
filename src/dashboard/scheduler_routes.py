"""
Scheduler API routes - Targets, Templates, Bindings, Schedule, Logs
"""
import asyncio
import json
import sqlite3
import structlog
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request

from src.dashboard.auth_access import dashboard_api_authorized
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.core.datetime_utc import utc_now_naive
from src.core.database import get_db_context
from src.core.models import Account, AccountStatus
from src.core.datetime_utc import to_utc_iso_z, utc_day_bounds_naive
from src.core.scheduler_models import (
    ChatTarget, AccountTargetBinding, MessageTemplate,
    ScheduleProfile, ScheduleRule, ScheduledJob, MessageDelivery,
    MessageType, JobStatus, AccountTargetMembershipProbe,
    AccountReadinessSnapshot,
    SCHEDULED_JOB_OPERATOR_SEND_TEST_MARKER,
    SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
)
from src.scheduler.renderer import render_template
from src.scheduler.production_insights import (
    all_accounts_reputation,
    batch_target_quality,
    compute_target_quality,
    count_bad_targets,
    count_risky_accounts,
)
from src.scheduler.pacing import get_send_pacing_decision
from src.clients.target_health import (
    classify_target,
    HEALTH_INVALID,
    HEALTH_NEEDS_REPAIR,
    merged_target_health_row,
    is_health_allowed_for_send,
    is_health_allowed_for_binding,
)

logger = structlog.get_logger(__name__)

# Upper bound for one membership-check batch (sequential Telethon RPCs per target).
# Upper bound kept for logging / legacy; actual limits are per-target + wall in
# ``check_targets_membership_sequential`` (membership_check.py).
MEMBERSHIP_PROBE_TIMEOUT_SEC = 120.0

scheduler_api = Blueprint('scheduler_api', __name__, url_prefix='/api/v1')


@scheduler_api.before_request
def scheduler_api_require_dashboard_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401
    from src.dashboard.scheduler_mutations import check_scheduler_mutation_allowed

    blocked = check_scheduler_mutation_allowed()
    if blocked is not None:
        return blocked


def _remove_target_id_from_schedule_rules(db, target_id: int) -> int:
    """Strip ``target_id`` from ONLY_SELECTED JSON lists. Returns rules updated."""
    updated = 0
    tid = int(target_id)
    for rule in db.query(ScheduleRule).all():
        raw = rule.selected_target_ids_json
        if not raw:
            continue
        try:
            ids = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ids, list) or tid not in [int(x) for x in ids]:
            continue
        new_ids = [x for x in ids if int(x) != tid]
        rule.selected_target_ids_json = json.dumps(new_ids) if new_ids else None
        updated += 1
    return updated


def _delete_chat_target_cascade(db, target_id: int) -> dict:
    """
    Manual cascade delete for ``chat_targets``.

    **Never** use ``session.delete(chat_target_instance)`` here: SQLAlchemy will try
    to nullify ``message_deliveries.target_id`` on related rows in the session,
    which violates NOT NULL and raises ``IntegrityError``.

    Order (explicit ``DELETE`` only, no FK nulling):
      1. message_deliveries
      2. scheduled_jobs
      3. message_templates (by ``target_id``, then by ``binding_id`` for this target)
      4. account_target_bindings
      5. schedule_rules JSON cleanup
      6. chat_targets (bulk delete, not ORM instance delete)
    """
    tid = int(target_id)
    rows_deleted: dict = {}

    bind_ids = [
        b.id for b in db.query(AccountTargetBinding).filter(
            AccountTargetBinding.target_id == tid
        ).all()
    ]

    # 1 — message_deliveries (NOT NULL target_id: must DELETE, never NULL)
    n = db.query(MessageDelivery).filter(MessageDelivery.target_id == tid).delete(
        synchronize_session=False
    )
    rows_deleted["message_deliveries"] = int(n or 0)
    db.flush()

    # 2 — scheduled_jobs
    n = db.query(ScheduledJob).filter(ScheduledJob.target_id == tid).delete(
        synchronize_session=False
    )
    rows_deleted["scheduled_jobs"] = int(n or 0)
    db.flush()

    # 3 — message_templates: this target, then binding-scoped rows (same logical step)
    n_t = db.query(MessageTemplate).filter(MessageTemplate.target_id == tid).delete(
        synchronize_session=False
    )
    rows_deleted["message_templates_target_id"] = int(n_t or 0)
    n_b = 0
    if bind_ids:
        n_b = db.query(MessageTemplate).filter(
            MessageTemplate.binding_id.in_(bind_ids)
        ).delete(synchronize_session=False)
    rows_deleted["message_templates_binding_id"] = int(n_b or 0)
    db.flush()

    # 4 — account_target_bindings
    n = db.query(AccountTargetBinding).filter(AccountTargetBinding.target_id == tid).delete(
        synchronize_session=False
    )
    rows_deleted["account_target_bindings"] = int(n or 0)
    db.flush()

    # 5 — schedule_rules (JSON, not FK)
    rules_updated = _remove_target_id_from_schedule_rules(db, tid)
    rows_deleted["schedule_rules_json_lists_updated"] = int(rules_updated)
    db.flush()

    # 6 — chat_targets (bulk DELETE avoids ORM nullify of children)
    n = db.query(ChatTarget).filter(ChatTarget.id == tid).delete(
        synchronize_session=False
    )
    rows_deleted["chat_targets"] = int(n or 0)

    deleted = rows_deleted["chat_targets"] > 0
    logger.info(
        "chat_target_manual_cascade",
        target_id=tid,
        rows_deleted=rows_deleted,
    )
    return {
        "deleted": deleted,
        "rows_deleted": rows_deleted,
        "rules_updated": rules_updated,
    }


# ============ Join Telegram targets for an account (UI helper) ============
@scheduler_api.route('/scheduler/join-targets', methods=['POST'])
def join_targets():
    """
    For each (account_id, target_id) pair, attempt to make the account a
    writable member of the target. Used by the Scheduler UI right after a
    campaign is saved so an account is actually subscribed to the groups it
    will post to. Does NOT modify scheduler/worker logic — it only joins
    chats and (when the join confirms posting is impossible) flips
    ``binding.can_post`` to False, which is the same write the executor
    would do later anyway after a ChatWriteForbidden.
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get('account_id')
    target_ids = data.get('target_ids') or []
    if not account_id or not isinstance(target_ids, list) or not target_ids:
        return jsonify({'error': 'account_id and non-empty target_ids required'}), 400
    try:
        account_id = int(account_id)
        target_ids = [int(t) for t in target_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'account_id and target_ids must be integers'}), 400
    if len(target_ids) > 50:
        return jsonify({'error': 'Maximum 50 targets per request'}), 400

    from src.clients.joiner import (
        join_target_for_account,
        STATUS_ALREADY_JOINED,
        STATUS_JOINED,
        STATUS_JOIN_REQUESTED,
    )
    from src.clients.manager import client_manager as _client_manager
    from src.clients import readiness_store

    async def _run():
        results = []
        for tid in target_ids:
            results.append(await join_target_for_account(account_id, tid))
        return results

    rows = []
    try:
        rows = asyncio.run(_run())
    finally:
        async def _drop():
            await _client_manager.remove_account(account_id)

        try:
            asyncio.run(_drop())
        except Exception:
            logger.warning("remove_account_after_join_targets_failed", account_id=account_id)

    if any(
        r.get("status") in (
            STATUS_JOINED,
            STATUS_ALREADY_JOINED,
            STATUS_JOIN_REQUESTED,
        )
        for r in rows
    ):
        with get_db_context() as db:
            readiness_store.mark_account_ready_after_success(
                db, account_id, "join_targets_ok",
            )

    return jsonify({'account_id': account_id, 'results': rows})


def _scheduler_row_counts_for_account(db, account_id: int) -> dict:
    """Non-destructive counts for orphan cleanup preview."""
    aid = int(account_id)
    return {
        "bindings": db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == aid
        ).count(),
        "scheduled_jobs": db.query(ScheduledJob).filter(
            ScheduledJob.account_id == aid
        ).count(),
        "message_templates": db.query(MessageTemplate).filter(
            MessageTemplate.account_id == aid
        ).count(),
        "schedule_rules": db.query(ScheduleRule).filter(
            ScheduleRule.account_id == aid
        ).count(),
        "schedule_profile": 1 if db.query(ScheduleProfile).filter(
            ScheduleProfile.account_id == aid
        ).first() else 0,
        "membership_probes": db.query(AccountTargetMembershipProbe).filter(
            AccountTargetMembershipProbe.account_id == aid
        ).count(),
    }


def _cleanup_orphan_scheduler_for_account(db, account_id: int) -> dict:
    """
    Remove scheduler DB rows for one account when it has **zero** target bindings.

    Does **not** delete ``Account`` (Telegram identity) or ``ChatTarget`` rows.
    Deletes: pending/future jobs, account-scoped templates, rules, profile,
    membership probe cache rows.

    ``MessageDelivery`` audit rows are kept.
    """
    aid = int(account_id)
    bind_n = db.query(AccountTargetBinding).filter(
        AccountTargetBinding.account_id == aid
    ).count()
    if bind_n > 0:
        raise ValueError(f"refusing cleanup: account has {bind_n} binding(s)")

    deleted: dict = {}

    n = db.query(AccountTargetMembershipProbe).filter(
        AccountTargetMembershipProbe.account_id == aid
    ).delete(synchronize_session=False)
    deleted["account_target_membership_probes"] = int(n or 0)
    db.flush()

    n = db.query(ScheduledJob).filter(ScheduledJob.account_id == aid).delete(
        synchronize_session=False
    )
    deleted["scheduled_jobs"] = int(n or 0)
    db.flush()

    n = db.query(MessageTemplate).filter(MessageTemplate.account_id == aid).delete(
        synchronize_session=False
    )
    deleted["message_templates"] = int(n or 0)
    db.flush()

    n = db.query(ScheduleRule).filter(ScheduleRule.account_id == aid).delete(
        synchronize_session=False
    )
    deleted["schedule_rules"] = int(n or 0)
    db.flush()

    prof = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == aid).first()
    if prof:
        db.delete(prof)
        deleted["schedule_profiles"] = 1
    else:
        deleted["schedule_profiles"] = 0
    db.flush()

    logger.info("orphan_scheduler_cleanup", account_id=aid, deleted=deleted)
    return deleted


@scheduler_api.route('/scheduler/orphan-profile/<int:account_id>', methods=['POST'])
def cleanup_orphan_scheduler_profile(account_id: int):
    """
    Operator cleanup: strip scheduler configuration for an account that has
    **no** ``account_target_bindings``. Never deletes the Telegram ``Account``.

    Body: ``{ "dry_run": true }`` — return counts only. ``dry_run`` false or
    omitted — perform delete.
    """
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run"))
    try:
        with get_db_context() as db:
            counts = _scheduler_row_counts_for_account(db, account_id)
            if counts["bindings"] > 0:
                return jsonify({
                    "error": "Account has target bindings — not an orphan profile",
                    "counts": counts,
                }), 400
            if dry_run:
                return jsonify({"account_id": account_id, "dry_run": True, "counts": counts})
            deleted = _cleanup_orphan_scheduler_for_account(db, account_id)
            return jsonify({"account_id": account_id, "success": True, "deleted": deleted})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except SQLAlchemyError as e:
        logger.exception("orphan_scheduler_cleanup_failed", account_id=account_id)
        return jsonify({"error": "database error", "detail": str(e)}), 500


# ============ Account readiness (read-only) ============
@scheduler_api.route('/accounts/readiness', methods=['GET'])
def accounts_readiness():
    """
    Per-account Telegram session status for operators.
    Use ?deep=1 to connect and check authorization (contacts Telegram; slower).
    Use ?account_id=<int> to check only one row (recommended after picking an account).

    Shallow responses merge ``account_readiness_snapshots`` (cross-process, survives refresh).
    Deep checks persist snapshots with TTL (READY / TEMP_CONNECT / NOT_AUTHORIZED / ERROR).
    """
    deep = request.args.get('deep', '0').lower() in ('1', 'true', 'yes')
    account_id = request.args.get('account_id', type=int)
    from src.clients.manager import client_manager
    from src.clients import readiness_store

    async def _run():
        return await client_manager.collect_accounts_readiness(
            deep=deep, only_account_id=account_id,
        )

    rows = asyncio.run(_run())
    with get_db_context() as db:
        if deep:
            for row in rows:
                readiness_store.persist_from_manager_deep_row(db, row)
        # Always overlay cached snapshot fields so the UI can render READY vs STALE
        # without forcing deep checks.
        rows = readiness_store.apply_cached_readiness_to_rows(db, rows)
        # Enrich with unified campaign readiness fields (purpose + account status).
        ids = [int(r.get("account_id")) for r in rows if r and r.get("account_id") is not None]
        acc_map = {}
        if ids:
            for a in db.query(Account).filter(Account.id.in_(ids)).all():
                st = a.status.value if hasattr(a.status, "value") else str(a.status)
                acc_map[int(a.id)] = {"purpose": getattr(a, "purpose", None) or "both", "status": st}
        now_u = datetime.now(timezone.utc)
        for r in rows:
            if not r or r.get("account_id") is None:
                continue
            aid = int(r.get("account_id"))
            meta = acc_map.get(aid, {})
            r["purpose"] = meta.get("purpose") or "both"
            r["account_status"] = meta.get("status") or None
            # readiness_age_sec for operator clarity
            ca = r.get("readiness_checked_at")
            age = None
            try:
                if ca:
                    dt = datetime.fromisoformat(str(ca).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age = max(0, int((now_u - dt.astimezone(timezone.utc)).total_seconds()))
            except Exception:
                age = None
            r["readiness_age_sec"] = age
            r.update(
                readiness_store.compute_campaign_ready_fields(
                    account_status=r.get("account_status"),
                    purpose=r.get("purpose"),
                    readiness_status=r.get("readiness_status"),
                    readiness_state=r.get("readiness_state"),
                    readiness_failure_kind=r.get("readiness_failure_kind"),
                    error=r.get("error"),
                )
            )
        from src.recovery.p9_34_freshness_stability import enrich_scheduler_readiness_row_p9_34

        for r in rows:
            enrich_scheduler_readiness_row_p9_34(db, r)
    return jsonify({'accounts': rows, 'deep': deep, 'account_id': account_id})


def _norm_joined_username_key(username):
    if not username or not str(username).strip():
        return None
    return str(username).strip().lstrip("@").lower()


def _find_chat_target_by_normalized_username(db, username_key: str):
    """Return an existing ``ChatTarget`` whose username normalizes to ``username_key`` (lower, no @)."""
    if not username_key:
        return None
    return (
        db.query(ChatTarget)
        .filter(
            ChatTarget.username.isnot(None),
            or_(
                func.lower(ChatTarget.username) == username_key,
                func.lower(ChatTarget.username) == ("@" + username_key),
            ),
        )
        .first()
    )


def _find_chat_target_for_telegram_joined_row(db, tg_entity_id, peer_id, username_key):
    """Match a Telethon dialog row to an existing ``ChatTarget`` (username or cached ``tg_id``)."""
    if username_key:
        t = (
            db.query(ChatTarget)
            .filter(
                ChatTarget.username.isnot(None),
                or_(
                    func.lower(ChatTarget.username) == username_key,
                    func.lower(ChatTarget.username) == ("@" + username_key),
                ),
            )
            .first()
        )
        if t:
            return t
    for key in (tg_entity_id, peer_id):
        if key is None:
            continue
        try:
            v = int(key)
        except (TypeError, ValueError):
            continue
        t = db.query(ChatTarget).filter(ChatTarget.tg_id == v).first()
        if t:
            return t
    return None


def _enrich_joined_groups_for_account(db, account_id: int, groups: list) -> list:
    aid = int(account_id)
    bindings = db.query(AccountTargetBinding).filter(AccountTargetBinding.account_id == aid).all()
    binding_by_target = {b.target_id: b.id for b in bindings}
    bound_ids = set(binding_by_target.keys())
    out = []
    for g in groups:
        uk = _norm_joined_username_key(g.get("username"))
        t = _find_chat_target_for_telegram_joined_row(
            db, g.get("tg_entity_id"), g.get("peer_id"), uk,
        )
        tid = int(t.id) if t else None
        out.append({
            **g,
            "scheduler_target_id": tid,
            "scheduler_bound": bool(tid and tid in bound_ids),
            "binding_id": binding_by_target.get(tid) if tid else None,
        })
    return out


@scheduler_api.route('/accounts/<int:account_id>/telegram-joined-groups', methods=['GET'])
def account_telegram_joined_groups(account_id: int):
    """
    Read-only: list groups/channels/supergroups from Telegram ``iter_dialogs`` for
    one account (no auto-import). Each row includes whether it maps to a
    ``chat_targets`` row and whether this account already has a binding.
    """
    limit = request.args.get("limit", default=300, type=int) or 300
    limit = max(1, min(int(limit), 500))
    with get_db_context() as db:
        acc = db.query(Account).filter(Account.id == account_id).first()
        if not acc:
            return jsonify({"error": "Account not found"}), 404
    from src.clients.manager import client_manager
    from src.dashboard.routes import run_async

    raw: dict = {}
    try:
        raw = run_async(client_manager.list_joined_groups_channels(account_id, limit=limit))
    finally:
        try:

            async def _drop():
                await client_manager.remove_account(account_id)

            asyncio.run(_drop())
        except Exception:
            logger.warning("remove_account_after_telegram_joined_groups_failed", account_id=account_id)

    if raw.get("error"):
        return jsonify({"error": raw["error"], "groups": []}), 502
    groups = raw.get("groups") or []
    with get_db_context() as db:
        enriched = _enrich_joined_groups_for_account(db, account_id, groups)
    return jsonify({
        "account_id": account_id,
        "total": len(enriched),
        "groups": enriched,
        "dialogs_scanned": raw.get("dialogs_scanned"),
        "scan_capped": raw.get("scan_capped"),
        "max_dialog_scans": raw.get("max_dialog_scans"),
    })


@scheduler_api.route('/accounts/<int:account_id>/telegram-joined-groups/bind', methods=['POST'])
def account_telegram_bind_joined_groups(account_id: int):
    """
    Optional operator action: create ``ChatTarget`` rows (when missing) and
    ``AccountTargetBinding`` for selected Telegram-joined rows. Does not join
    Telegram or touch the scheduler engine — same validation as ``POST /bindings``.
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "non-empty items[] required"}), 400
    if len(items) > 30:
        return jsonify({"error": "Maximum 30 items per request"}), 400

    results = []
    for raw in items:
        if not isinstance(raw, dict):
            results.append({"ok": False, "error": "each item must be an object"})
            continue
        try:
            with get_db_context() as db:
                acc = db.query(Account).filter(Account.id == account_id).first()
                if not acc:
                    results.append({"ok": False, "error": "Account not found"})
                    continue

                existing_tid = raw.get("scheduler_target_id")
                t = None
                created_new = False
                if existing_tid is not None:
                    t = db.query(ChatTarget).filter(ChatTarget.id == int(existing_tid)).first()
                if t is None:
                    uk = _norm_joined_username_key(raw.get("username"))
                    t = _find_chat_target_for_telegram_joined_row(
                        db,
                        raw.get("tg_entity_id"),
                        raw.get("peer_id"),
                        uk,
                    )
                if t is None:
                    tg_entity_id = raw.get("tg_entity_id")
                    un_raw = raw.get("username")
                    un_key = _norm_joined_username_key(un_raw)
                    if un_key:
                        t = _find_chat_target_by_normalized_username(db, un_key)
                    un_norm = un_key if un_key else ((str(un_raw).strip().lstrip("@") if un_raw else None) or None)
                    if t is None and tg_entity_id is None and not un_norm:
                        results.append({"ok": False, "error": "username or tg_entity_id required to create target"})
                        continue
                    if t is None:
                        title = (raw.get("title") or "").strip() or None
                        chat_type = (raw.get("chat_type") or "supergroup").strip().lower()
                        if chat_type not in ("channel", "group", "supergroup"):
                            chat_type = "supergroup"
                        try:
                            tg_int = int(tg_entity_id) if tg_entity_id is not None else None
                        except (TypeError, ValueError):
                            results.append({"ok": False, "error": "invalid tg_entity_id"})
                            continue
                        cand = {
                            "username": un_norm,
                            "invite_link": None,
                            "tg_id": tg_int,
                            "chat_type": chat_type,
                        }
                        h0 = classify_target(cand)
                        if h0["health"] == HEALTH_INVALID:
                            results.append({"ok": False, "error": h0.get("reason") or "invalid target shape"})
                            continue
                        if not is_health_allowed_for_binding(h0["health"]):
                            results.append({
                                "ok": False,
                                "error": h0.get("reason") or "target not allowed for binding",
                                "health": h0.get("health"),
                            })
                            continue
                        t = ChatTarget(
                            username=un_norm,
                            invite_link=None,
                            tg_id=tg_int,
                            title=(title[:255] if title else None),
                            chat_type=chat_type,
                        )
                        db.add(t)
                        db.flush()
                        created_new = True

                mh = merged_target_health_row(db, t, int(account_id))
                if not is_health_allowed_for_binding(mh["health"]):
                    if created_new:
                        db.delete(t)
                    results.append({
                        "ok": False,
                        "error": mh.get("health_reason") or "Target not usable for binding",
                        "health": mh.get("health"),
                    })
                    continue

                exists = (
                    db.query(AccountTargetBinding)
                    .filter(
                        AccountTargetBinding.account_id == int(account_id),
                        AccountTargetBinding.target_id == int(t.id),
                    )
                    .first()
                )
                if exists:
                    results.append({
                        "ok": True,
                        "skipped": True,
                        "target_id": t.id,
                        "binding_id": exists.id,
                    })
                    continue

                b = AccountTargetBinding(
                    account_id=int(account_id),
                    target_id=int(t.id),
                    can_post=True,
                    allowed_types="PROMO,INFO",
                )
                db.add(b)
                db.flush()
                results.append({
                    "ok": True,
                    "created_target": created_new,
                    "target_id": t.id,
                    "binding_id": b.id,
                })
        except IntegrityError as e:
            results.append({"ok": False, "error": "database conflict — row may already exist", "detail": str(e)})
        except (TypeError, ValueError) as e:
            results.append({"ok": False, "error": str(e)})

    return jsonify({"account_id": account_id, "results": results})


# ============ Stats ============
@scheduler_api.route('/stats', methods=['GET'])
def scheduler_stats():
    # UTC calendar day [start, end) — same basis as delivery JSON timestamps (``to_utc_iso_z``).
    today_start, tomorrow_start = utc_day_bounds_naive()
    from src.clients.readiness_store import (
        STAT_NOT_AUTH,
        STAT_READY,
        STAT_TEMP,
        snapshot_row_valid,
    )

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_db_context() as db:
        _st = func.lower(func.trim(MessageDelivery.status))
        sent_today = db.query(MessageDelivery).filter(
            _st == "sent",
            MessageDelivery.created_at >= today_start,
            MessageDelivery.created_at < tomorrow_start,
        ).count()
        failed_today = db.query(MessageDelivery).filter(
            _st == "failed",
            MessageDelivery.created_at >= today_start,
            MessageDelivery.created_at < tomorrow_start,
        ).count()
        active_accounts = db.query(ScheduleProfile).filter(
            ScheduleProfile.is_enabled == True
        ).count()
        target_groups = db.query(ChatTarget).count()
        pending_jobs = db.query(ScheduledJob).filter(
            ScheduledJob.status == JobStatus.PENDING.value
        ).count()

        accounts_ready = accounts_retry = accounts_relogin = 0
        for snap in db.query(AccountReadinessSnapshot).all():
            if not snapshot_row_valid(snap, now_naive):
                continue
            if snap.status == STAT_READY:
                accounts_ready += 1
            elif snap.status == STAT_TEMP:
                accounts_retry += 1
            elif snap.status == STAT_NOT_AUTH:
                accounts_relogin += 1

        unsafe_targets = 0
        for ct in db.query(ChatTarget).all():
            if classify_target(ct).get("health") == HEALTH_NEEDS_REPAIR:
                unsafe_targets += 1

        failed_rows = (
            db.query(MessageDelivery.error_code, func.count(MessageDelivery.id))
            .filter(
                _st == "failed",
                MessageDelivery.created_at >= today_start,
                MessageDelivery.created_at < tomorrow_start,
            )
            .group_by(MessageDelivery.error_code)
            .all()
        )
        failed_today_by_error_code = {
            (str(code).strip() if code is not None and str(code).strip() else "(none)"): int(cnt or 0)
            for code, cnt in failed_rows
        }

        risk_buckets = count_risky_accounts(db, now=now_naive)
        bad_targets_n = count_bad_targets(db, now=now_naive)

    return jsonify({
        'sent_today': sent_today,
        'failed_today': failed_today,
        'active_accounts': active_accounts,
        'target_groups': target_groups,
        'pending_jobs': pending_jobs,
        'production': {
            'accounts_readiness_valid': {
                'READY': accounts_ready,
                'RETRY': accounts_retry,
                'RELOGIN': accounts_relogin,
            },
            'unsafe_targets': unsafe_targets,
            'failed_today_by_error_code': failed_today_by_error_code,
            'accounts_reputation_buckets_24h': risk_buckets,
            'bad_or_repair_targets_7d': bad_targets_n,
        },
        'meta': {
            'utc_day_start': to_utc_iso_z(today_start),
            'utc_day_end_exclusive': to_utc_iso_z(tomorrow_start),
        },
    })


@scheduler_api.route('/accounts/reputation', methods=['GET'])
def list_accounts_reputation():
    """Read-only 24h delivery-derived reputation per account (no migrations)."""
    with get_db_context() as db:
        rows = all_accounts_reputation(db)
    return jsonify({"accounts": rows})


@scheduler_api.route('/targets/quality', methods=['GET'])
def list_targets_quality():
    """
    Read-only 7d delivery-derived quality per target.

    Optional ``account_id`` scopes merged-health hints used in quality rules.
    """
    account_id = request.args.get("account_id", type=int)
    with get_db_context() as db:
        tids = [t.id for t in db.query(ChatTarget).order_by(ChatTarget.id).all()]
        qmap = batch_target_quality(db, tids, account_id=account_id)
        out = [qmap[int(tid)] for tid in tids if int(tid) in qmap]
    return jsonify({"targets": out, "account_id": account_id})


@scheduler_api.route('/targets/<int:target_id>/quality', methods=['GET'])
def get_target_quality(target_id: int):
    account_id = request.args.get("account_id", type=int)
    with get_db_context() as db:
        row = compute_target_quality(db, int(target_id), account_id=account_id)
    return jsonify(row)


_ALLOWED_HEALTH_FILTER = frozenset({
    "sendable", "joinable", "invalid", "banned", "no_permission",
    "unresolved_entity", "pending_approval", "needs_repair",
})


@scheduler_api.route('/targets', methods=['GET'])
def list_targets():
    """
    Optional query params (additive, backward compatible):
      exclude_invalid=1 — omit intrinsically invalid targets
      safe_only=1 — omit targets not safe to schedule (banned, no_permission,
                    unresolved_entity, invalid); keeps sendable/joinable/pending_approval
      health=<code> — keep only that merged health bucket
      account_id=<int> — scope delivery/binding signals to one account (recommended for Campaign Setup)
      include_quality=1 — add compact ``quality_*`` fields (7d delivery-derived risk)
    """
    exclude_invalid = request.args.get("exclude_invalid", "").lower() in ("1", "true", "yes")
    safe_only = request.args.get("safe_only", "").lower() in ("1", "true", "yes")
    health_only = (request.args.get("health") or "").strip().lower() or None
    account_id = request.args.get("account_id", type=int)
    include_quality = request.args.get("include_quality", "").lower() in ("1", "true", "yes")

    if health_only and health_only not in _ALLOWED_HEALTH_FILTER:
        return jsonify({"error": "unknown health filter", "allowed": sorted(_ALLOWED_HEALTH_FILTER)}), 400

    _NOT_SAFE = frozenset({
        HEALTH_INVALID, HEALTH_NEEDS_REPAIR, "banned", "no_permission", "unresolved_entity",
    })

    with get_db_context() as db:
        targets = db.query(ChatTarget).all()
        rows = []
        for t in targets:
            intrinsic = classify_target(t)
            if exclude_invalid and intrinsic.get("health") == HEALTH_INVALID:
                continue
            mh = merged_target_health_row(db, t, account_id)
            h = mh["health"]
            if safe_only and h in _NOT_SAFE:
                continue
            if health_only and h != health_only:
                continue
            rows.append({
                "id": t.id,
                "tg_id": t.tg_id,
                "username": t.username,
                "invite_link": t.invite_link,
                "title": t.title,
                "chat_type": t.chat_type,
                "is_verified": t.is_verified,
                "verification_trusted": bool(
                    t.is_verified
                    and h != HEALTH_INVALID
                    and h != HEALTH_NEEDS_REPAIR
                    and h not in ("banned", "no_permission", "unresolved_entity")
                ),
                "intrinsic_health": mh.get("intrinsic_health"),
                "intrinsic_health_reason": mh.get("intrinsic_health_reason"),
                "operational_health": mh.get("operational_health"),
                "operational_health_reason": mh.get("operational_health_reason"),
                "proven_send_recent": mh.get("proven_send_recent", False),
                "health": h,
                "health_reason": mh.get("health_reason"),
            })
        if include_quality and rows:
            qmap = batch_target_quality(db, [int(r["id"]) for r in rows], account_id=account_id)
            for r in rows:
                q = qmap.get(int(r["id"])) or {}
                r["quality_risk"] = q.get("risk_level")
                r["quality_sent_7d"] = q.get("recent_sent_count_7d")
                r["quality_failed_7d"] = q.get("recent_failed_count_7d")
                r["quality_success_rate_7d"] = q.get("success_rate_7d")
        return jsonify(rows)


@scheduler_api.route('/targets', methods=['POST'])
def create_target():
    """
    Reject obviously-invalid targets up front (t.me/c/<id>/<msg>, message
    permalinks, no usable handle, peer-user). The classifier is the same one
    the UI uses, so the rejection reason is identical to what would be shown
    inline for an existing bad row.
    """
    data = request.get_json() or {}
    candidate = {
        "username": data.get("username"),
        "invite_link": data.get("invite_link"),
        "tg_id": data.get("tg_id"),
        "chat_type": data.get("chat_type", "channel"),
    }
    health = classify_target(candidate)
    if health["health"] == HEALTH_INVALID:
        return jsonify({
            "error": health["reason"],
            "health": HEALTH_INVALID,
        }), 400

    uk = _norm_joined_username_key(data.get("username"))
    username_store = uk if uk else (
        (str(candidate["username"]).strip().lstrip("@") or None)
        if candidate.get("username") is not None and str(candidate["username"]).strip()
        else None
    )

    with get_db_context() as db:
        if uk:
            existing = _find_chat_target_by_normalized_username(db, uk)
            if existing:
                intr = classify_target(existing)
                return jsonify({
                    "id": existing.id,
                    "success": True,
                    "reused": True,
                    "health": intr["health"],
                    "health_reason": intr["reason"],
                })
        t = ChatTarget(
            username=username_store,
            invite_link=candidate["invite_link"],
            tg_id=candidate["tg_id"],
            chat_type=candidate["chat_type"],
        )
        db.add(t)
        db.flush()  # Persist and assign ID (refresh fails on pending instances)
        return jsonify({
            "id": t.id,
            "success": True,
            "health": health["health"],
            "health_reason": health["reason"],
        })


@scheduler_api.route('/targets/<int:target_id>/clear-cached-id', methods=['POST'])
def clear_target_cached_tg_id(target_id: int):
    """
    Admin-safe: clear only ``tg_id`` on a ``ChatTarget`` (keeps username, invite, title).

    Use when Telegram entity resolution fails (stale PeerUser / wrong cached id)
    while @username or invite is still correct.
    """
    tid = int(target_id)
    try:
        with get_db_context() as db:
            t = db.query(ChatTarget).filter(ChatTarget.id == tid).first()
            if not t:
                return jsonify({"error": "Target not found"}), 404
            if not (t.username or t.invite_link):
                return jsonify({
                    "error": "Refusing to clear tg_id: target has no username or invite_link to resolve by",
                    "target_id": tid,
                }), 400
            old = t.tg_id
            t.tg_id = None
            db.flush()
            return jsonify({
                "success": True,
                "target_id": tid,
                "cleared_tg_id": old,
                "hint": "Re-save / re-run membership so Telegram can repopulate tg_id from @username or invite.",
            })
    except SQLAlchemyError as e:
        logger.exception("clear_cached_tg_id_failed", target_id=tid)
        return jsonify({"error": "database error", "detail": str(e)}), 500


@scheduler_api.route('/targets/duplicate-report', methods=['GET'])
def targets_duplicate_report():
    """
    Admin-safe read-only report: duplicate ``chat_targets`` rows by normalized
    @username or identical ``tg_id``. Does not delete or merge.
    """
    from src.clients.readiness_store import detect_duplicate_targets

    with get_db_context() as db:
        report = detect_duplicate_targets(db)
    return jsonify(report)


@scheduler_api.route('/targets/dedupe/dry-run', methods=['POST'])
def targets_dedupe_dry_run():
    """
    Planned merge operations for duplicate ``chat_targets`` rows (no mutations).
    Body optional: ``canonical_target_id`` + ``duplicate_target_ids`` + ``group_key``
    to plan a single merge; empty body runs all duplicate groups from the report.
    """
    from src.clients.target_dedupe import build_plan_for_target_ids, dry_run_all_duplicate_groups

    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        cid = data.get("canonical_target_id")
        dups = data.get("duplicate_target_ids")
        if cid is not None and isinstance(dups, list) and dups:
            try:
                ids = [int(cid)] + [int(x) for x in dups]
            except (TypeError, ValueError):
                return jsonify({"error": "canonical_target_id and duplicate_target_ids must be integers"}), 400
            gk = data.get("group_key") or "custom"
            plan = build_plan_for_target_ids(db, ids, group_key=str(gk))
            return jsonify({"plans": [plan]})
        return jsonify(dry_run_all_duplicate_groups(db))


@scheduler_api.route('/targets/dedupe/apply', methods=['POST'])
def targets_dedupe_apply():
    """
    Apply a safe merge of duplicate targets into a canonical row.

    Body: ``canonical_target_id``, ``duplicate_target_ids``, ``dry_run`` (default true).
    """
    from src.core.database import SessionLocal

    from src.clients.target_dedupe import apply_dedupe_merge

    data = request.get_json(silent=True) or {}
    canonical = data.get("canonical_target_id")
    dups = data.get("duplicate_target_ids")
    dry_run = data.get("dry_run", True)
    if dry_run is None:
        dry_run = True
    if canonical is None:
        return jsonify({"error": "canonical_target_id required"}), 400
    if not isinstance(dups, list) or not dups:
        return jsonify({"error": "duplicate_target_ids (non-empty list) required"}), 400
    try:
        canonical_i = int(canonical)
        dup_list = [int(x) for x in dups]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400

    db = SessionLocal()
    try:
        payload, code = apply_dedupe_merge(
            db,
            canonical_target_id=canonical_i,
            duplicate_target_ids=dup_list,
            dry_run=bool(dry_run),
        )
        if code >= 400:
            db.rollback()
            return jsonify(payload), code
        db.commit()
        return jsonify(payload), code
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@scheduler_api.route('/targets/membership-check', methods=['POST'])
def targets_membership_check():
    """
    Operator-triggered read-only Telegram membership probe for one account
    and a bounded list of targets. Does NOT join or mutate Telegram membership.

    Body: { "account_id": int, "target_ids": [int, ...], "force_refresh"?: bool }

    * DB membership cache is used only when ``account_readiness_snapshots`` shows READY.
    * Pooled Telethon client is always released in ``finally`` (web must not hold sessions).
    * Batch timeout returns HTTP 200 with ``timed_out`` + per-target retry rows (not 504).
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    target_ids = data.get("target_ids") or []
    force_refresh = bool(data.get("force_refresh"))

    if not account_id or not isinstance(target_ids, list) or not target_ids:
        return jsonify({"error": "account_id and non-empty target_ids required"}), 400
    try:
        account_id = int(account_id)
        target_ids = [int(t) for t in target_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "account_id and target_ids must be integers"}), 400

    if len(target_ids) > 25:
        return jsonify({"error": "Maximum 25 targets per membership-check request"}), 400

    from src.clients.membership_check import (
        DEFAULT_PROBE_CACHE_TTL_SEC,
        check_targets_membership_sequential,
        read_cached_probes,
        upsert_membership_probe,
    )
    from src.clients.manager import client_manager as _client_manager
    from src.clients import readiness_store
    from src.clients.session_resolve import human_message_for_code

    label_by_id: dict[int, str] = {}
    with get_db_context() as db:
        for t in db.query(ChatTarget).filter(ChatTarget.id.in_(target_ids)).all():
            label_by_id[int(t.id)] = (
                t.username or t.invite_link or (t.tg_id and str(t.tg_id)) or f"target #{t.id}"
            )

    trust_probe_cache = False
    with get_db_context() as db:
        trust_probe_cache = readiness_store.snapshot_ready_and_valid(db, account_id)

    pooled = False
    gate_err = None

    async def _gate():
        with get_db_context() as db:
            acc = db.query(Account).filter(Account.id == account_id).first()
        if not acc:
            return None, "account_not_found"
        return await _client_manager.add_account(acc)

    try:
        wrapper, gate_err = asyncio.run(_gate())
    except Exception:
        wrapper, gate_err = None, "failed_connect"

    if wrapper is not None:
        pooled = True

    def _release_pool() -> None:
        if not pooled:
            return

        async def _rm() -> None:
            await _client_manager.remove_account(account_id)

        try:
            asyncio.run(_rm())
        except Exception:
            logger.warning("remove_account_after_membership_failed", account_id=account_id)

    envelope_checked = to_utc_iso_z(datetime.now(timezone.utc))

    if wrapper is None:
        err_code = gate_err or "client_unavailable"
        with get_db_context() as db:
            if err_code == "unauthorized_session":
                readiness_store.mark_account_not_authorized(
                    db,
                    account_id,
                    human_message_for_code("unauthorized_session"),
                )
            else:
                detail = human_message_for_code(err_code) if err_code else "Connect failed"
                if err_code == "account_not_found":
                    detail = "Account not found"
                readiness_store.mark_account_temp_connect(db, account_id, detail, err_code)
        sig = "NOT_AUTHORIZED" if err_code == "unauthorized_session" else "TEMP_CONNECT"
        msg = human_message_for_code(err_code) if err_code else "Could not connect Telegram session"
        if err_code == "account_not_found":
            msg = "Account not found"
        results = [{
            "target_id": tid,
            "target_label": label_by_id.get(tid) or f"target #{tid}",
            "status": "error",
            "can_post": None,
            "message": msg,
            "error": err_code,
            "cached": False,
            "workflow_signal": sig,
        } for tid in target_ids]
        return jsonify({
            "account_id": account_id,
            "checked_at": envelope_checked,
            "cache_ttl_sec": DEFAULT_PROBE_CACHE_TTL_SEC,
            "results": results,
            "session_ok": False,
            "readiness_signal": sig,
        })

    try:
        cached: dict[int, dict] = {}
        if trust_probe_cache and not force_refresh:
            with get_db_context() as db:
                cached = read_cached_probes(
                    db, account_id, target_ids, ttl_sec=DEFAULT_PROBE_CACHE_TTL_SEC,
                )

        to_probe = [tid for tid in target_ids if tid not in cached]

        if not to_probe:
            results = []
            for tid in target_ids:
                row = dict(cached[tid])
                row["target_label"] = label_by_id.get(tid) or row.get("target_label") or f"target #{tid}"
                results.append(row)
            with get_db_context() as db:
                readiness_store.mark_account_ready_after_success(
                    db, account_id, "membership_cache_hit",
                )
            return jsonify({
                "account_id": account_id,
                "checked_at": envelope_checked,
                "cache_ttl_sec": DEFAULT_PROBE_CACHE_TTL_SEC,
                "results": results,
                "session_ok": True,
            })

        fresh_by_id: dict[int, dict] = {}
        timed_out = False
        fresh_list: list = []

        async def _run():
            return await check_targets_membership_sequential(account_id, to_probe)

        fresh_list = asyncio.run(_run())
        timeout_codes = {"target_timeout", "batch_wall_timeout"}
        timed_out = any(timeout_codes.intersection({str(r.get("error") or "")}) for r in fresh_list)
        had_probe_success = any(
            str(r.get("error") or "") not in timeout_codes
            and str(r.get("status") or "") not in ("", "error")
            for r in fresh_list
        )
        if timed_out and not had_probe_success:
            logger.warning(
                "membership_probe_all_timed_out",
                account_id=account_id,
                n_targets=len(to_probe),
            )
            with get_db_context() as db:
                readiness_store.mark_account_temp_connect(
                    db,
                    account_id,
                    "Membership probe: every target hit a time limit — retry with fewer targets",
                    "membership_timeout",
                )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for r in fresh_list:
            tid = int(r.get("target_id") or 0)
            r["cached"] = False
            r["checked_at"] = to_utc_iso_z(now)
            if not r.get("target_label"):
                r["target_label"] = label_by_id.get(tid) or r.get("target_label")
            fresh_by_id[tid] = r

        with get_db_context() as db:
            may_cache = readiness_store.snapshot_ready_and_valid(db, account_id)
            if may_cache:
                for r in fresh_list:
                    upsert_membership_probe(db, account_id, r, checked_at=now)
            if not (timed_out and not had_probe_success):
                readiness_store.mark_account_ready_after_success(
                    db, account_id, "membership_probe_ok",
                )

        results = []
        for tid in target_ids:
            if tid in cached:
                row = dict(cached[tid])
                row["target_label"] = label_by_id.get(tid) or row.get("target_label") or f"target #{tid}"
                results.append(row)
            elif tid in fresh_by_id:
                results.append(fresh_by_id[tid])
            else:
                results.append({
                    "target_id": tid,
                    "target_label": label_by_id.get(tid) or f"target #{tid}",
                    "status": "error",
                    "can_post": None,
                    "message": "Probe missing (unexpected)",
                    "error": "internal",
                })

        envelope_checked = to_utc_iso_z(datetime.now(timezone.utc))
        out = {
            "account_id": account_id,
            "checked_at": envelope_checked,
            "cache_ttl_sec": DEFAULT_PROBE_CACHE_TTL_SEC,
            "results": results,
            "session_ok": True,
        }
        if timed_out:
            out["timed_out"] = True
        return jsonify(out)
    finally:
        _release_pool()


@scheduler_api.route('/targets/<int:target_id>', methods=['DELETE'])
def delete_target(target_id):
    """
    Remove a chat target using explicit DELETEs in one transaction.
    Does not null FKs; never uses ORM ``delete(instance)`` on ``ChatTarget``.
    """
    try:
        with get_db_context() as db:
            exists = (
                db.query(ChatTarget.id)
                .filter(ChatTarget.id == int(target_id))
                .first()
            )
            if not exists:
                return jsonify({"error": "Target not found"}), 404
            meta = _delete_chat_target_cascade(db, target_id)
        if not meta.get("deleted"):
            return jsonify({
                "error": "Target was not removed",
                "detail": "No chat_targets row deleted after cascade",
                "hint": "Target id may have been removed concurrently.",
                "rows_deleted": meta.get("rows_deleted", {}),
            }), 409
        return jsonify({
            "success": True,
            "rows_deleted": meta.get("rows_deleted", {}),
            "rules_updated": meta.get("rules_updated", 0),
        })
    except (IntegrityError, sqlite3.IntegrityError) as e:
        orig = getattr(e, "orig", e)
        logger.warning("target_delete_integrity", target_id=target_id, error=str(e), orig=str(orig))
        return jsonify({
            "error": "Could not delete target — database constraint",
            "detail": str(orig),
            "hint": "Dependent rows may still reference this target; check logs and DB.",
        }), 409
    except SQLAlchemyError as e:
        logger.exception("target_delete_failed", target_id=target_id)
        return jsonify({
            "error": "Could not delete target",
            "detail": str(getattr(e, "orig", e) or e),
            "hint": "See server logs (chat_target_manual_cascade / target_delete_failed).",
        }), 409
    except Exception as e:
        logger.exception("target_delete_unexpected", target_id=target_id)
        return jsonify({
            "error": "Could not delete target",
            "detail": str(e),
            "hint": "Unexpected error — transaction rolled back; see server logs.",
        }), 409


@scheduler_api.route('/targets/<int:target_id>/verify', methods=['POST'])
def verify_target(target_id):
    """Mark target as operator-verified only when the row is not intrinsically invalid."""
    with get_db_context() as db:
        t = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()
        if not t:
            return jsonify({"error": "Target not found"}), 404
        h = classify_target(t)
        if h.get("health") == HEALTH_INVALID:
            return jsonify({
                "error": "Cannot verify this target — it is classified as invalid",
                "reason": h.get("reason"),
                "health": HEALTH_INVALID,
            }), 400
        if h.get("health") == HEALTH_NEEDS_REPAIR:
            return jsonify({
                "error": "Cannot verify — add @username or invite before marking verified",
                "reason": h.get("reason"),
                "health": HEALTH_NEEDS_REPAIR,
            }), 400
        t.is_verified = True
        t.verified_at = datetime.utcnow()
        return jsonify({"success": True, "is_verified": True})


@scheduler_api.route('/targets/<int:target_id>/repair', methods=['POST'])
def repair_target(target_id):
    """
    Operator-triggered cleanup: clear a bad cached ``tg_id`` / title so the next
    join/send uses username or invite. Does not delete the row.
    """
    data = request.get_json(silent=True) or {}
    clear_tg = data.get("clear_cached_chat_id", True)
    clear_title = data.get("clear_title", False)
    clear_verify = data.get("clear_verification", True)

    with get_db_context() as db:
        t = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()
        if not t:
            return jsonify({"error": "Target not found"}), 404
        if clear_tg:
            t.tg_id = None
        if clear_title:
            t.title = None
        if clear_verify:
            t.is_verified = False
            t.verified_at = None
        h = classify_target(t)
        return jsonify({
            "success": True,
            "health": h.get("health"),
            "health_reason": h.get("reason"),
            "tg_id": t.tg_id,
            "is_verified": t.is_verified,
        })


# ============ Bindings ============
@scheduler_api.route('/bindings', methods=['GET'])
def list_bindings():
    account_id = request.args.get("account_id", type=int)
    with get_db_context() as db:
        q = db.query(AccountTargetBinding)
        if account_id:
            q = q.filter(AccountTargetBinding.account_id == account_id)
        bindings = q.all()
        return jsonify([{
            "id": b.id,
            "account_id": b.account_id,
            "target_id": b.target_id,
            "can_post": b.can_post,
            "allowed_types": b.allowed_types,
            "daily_cap": b.daily_cap,
        } for b in bindings])


@scheduler_api.route('/bindings', methods=['POST'])
def create_binding():
    data = request.get_json() or {}
    account_id = data.get("account_id")
    target_id = data.get("target_id")
    if not account_id or not target_id:
        return jsonify({"error": "account_id and target_id required"}), 400
    with get_db_context() as db:
        tgt = db.query(ChatTarget).filter(ChatTarget.id == int(target_id)).first()
        if not tgt:
            return jsonify({"error": "Target not found"}), 404
        mh = merged_target_health_row(db, tgt, int(account_id))
        if not is_health_allowed_for_binding(mh["health"]):
            return jsonify({
                "error": mh.get("health_reason")
                or "Target is not usable for binding — fix health or remove this row",
                "health": mh.get("health"),
            }), 400
        exists = db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == account_id,
            AccountTargetBinding.target_id == target_id
        ).first()
        if exists:
            return jsonify({"error": "Binding already exists", "id": exists.id}), 400
        b = AccountTargetBinding(
            account_id=account_id,
            target_id=target_id,
            can_post=data.get("can_post", True),
            allowed_types=data.get("allowed_types", "PROMO,INFO"),
            daily_cap=data.get("daily_cap")
        )
        db.add(b)
        db.flush()
        return jsonify({"id": b.id, "success": True})


@scheduler_api.route('/bindings/<int:binding_id>', methods=['PUT'])
def update_binding(binding_id):
    data = request.get_json() or {}
    with get_db_context() as db:
        b = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == binding_id).first()
        if not b:
            return jsonify({"error": "Binding not found"}), 404
        for k in ["can_post", "allowed_types", "daily_cap"]:
            if k in data:
                setattr(b, k, data[k])
        return jsonify({"success": True})


@scheduler_api.route('/bindings/<int:binding_id>', methods=['DELETE'])
def delete_binding(binding_id):
    with get_db_context() as db:
        b = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == binding_id).first()
        if not b:
            return jsonify({"error": "Binding not found"}), 404
        db.delete(b)
        return jsonify({"success": True})


# ============ Templates ============
@scheduler_api.route('/templates', methods=['GET'])
def list_templates():
    msg_type = request.args.get("type")
    scope = request.args.get("scope")
    with get_db_context() as db:
        q = db.query(MessageTemplate)
        if msg_type:
            q = q.filter(MessageTemplate.type == msg_type)
        if scope:
            q = q.filter(MessageTemplate.scope == scope)
        templates = q.all()
        return jsonify([{
            "id": t.id,
            "type": t.type,
            "scope": t.scope,
            "account_id": t.account_id,
            "target_id": t.target_id,
            "binding_id": t.binding_id,
            "name": t.name,
            "body": t.body,
            "is_active": t.is_active,
            "weight": t.weight,
        } for t in templates])


@scheduler_api.route('/templates', methods=['POST'])
def create_template():
    data = request.get_json() or {}
    if not data.get("type") or not data.get("body"):
        return jsonify({"error": "type and body required"}), 400
    with get_db_context() as db:
        t = MessageTemplate(
            type=data["type"],
            scope=data.get("scope", "GLOBAL"),
            account_id=data.get("account_id"),
            target_id=data.get("target_id"),
            binding_id=data.get("binding_id"),
            name=data.get("name", "Template"),
            body=data["body"],
            is_active=data.get("is_active", True),
            weight=data.get("weight", 100)
        )
        db.add(t)
        db.flush()
        return jsonify({"id": t.id, "success": True})


@scheduler_api.route('/templates/<int:template_id>', methods=['PUT'])
def update_template(template_id):
    data = request.get_json() or {}
    with get_db_context() as db:
        t = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
        if not t:
            return jsonify({"error": "Template not found"}), 404
        for k in ["type", "scope", "name", "body", "is_active", "weight", "account_id", "target_id", "binding_id"]:
            if k in data:
                setattr(t, k, data[k])
        return jsonify({"success": True})


@scheduler_api.route('/templates/<int:template_id>', methods=['DELETE'])
def delete_template(template_id):
    with get_db_context() as db:
        t = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
        if not t:
            return jsonify({"error": "Template not found"}), 404
        db.delete(t)
        return jsonify({"success": True})


@scheduler_api.route('/templates/preview', methods=['POST'])
def preview_template():
    data = request.get_json() or {}
    body = data.get("body")
    if not body:
        return jsonify({"error": "body required"}), 400
    rendered = render_template(
        body,
        account_name=data.get("account_name", "Account"),
        chat_title=data.get("chat_title", "Chat"),
    )
    return jsonify({"rendered": rendered})


# ============ Schedule Profile ============
@scheduler_api.route('/schedule/profiles', methods=['GET'])
def list_schedule_profiles():
    with get_db_context() as db:
        profiles = db.query(ScheduleProfile).all()
        return jsonify([{
            "id": p.id,
            "account_id": p.account_id,
            "is_enabled": p.is_enabled,
            "timezone": p.timezone,
            "min_interval_sec": p.min_interval_sec,
            "daily_cap_total": p.daily_cap_total,
            "jitter_sec": p.jitter_sec,
        } for p in profiles])


@scheduler_api.route('/schedule/profile/<int:account_id>', methods=['GET'])
def get_schedule_profile(account_id):
    with get_db_context() as db:
        p = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == account_id).first()
        if not p:
            return jsonify({"account_id": account_id, "is_enabled": False})
        return jsonify({
            "id": p.id,
            "account_id": p.account_id,
            "is_enabled": p.is_enabled,
            "timezone": p.timezone,
            "min_interval_sec": p.min_interval_sec,
            "daily_cap_total": p.daily_cap_total,
            "daily_cap_promo": p.daily_cap_promo,
            "daily_cap_info": p.daily_cap_info,
            "jitter_sec": p.jitter_sec,
            "quiet_hours_json": p.quiet_hours_json,
        })


@scheduler_api.route('/schedule/profile/<int:account_id>', methods=['PUT'])
def update_schedule_profile(account_id):
    data = request.get_json() or {}
    with get_db_context() as db:
        p = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == account_id).first()
        if not p:
            p = ScheduleProfile(account_id=account_id)
            db.add(p)
            db.flush()
        for k in ["is_enabled", "timezone", "min_interval_sec", "daily_cap_total",
                  "daily_cap_promo", "daily_cap_info", "jitter_sec", "quiet_hours_json"]:
            if k in data:
                setattr(p, k, data[k])
        return jsonify({"success": True})


# ============ Schedule Rules ============
@scheduler_api.route('/schedule/rules', methods=['GET'])
def list_all_schedule_rules():
    with get_db_context() as db:
        rules = db.query(ScheduleRule).all()
        return jsonify([{
            "id": r.id,
            "account_id": r.account_id,
            "type": r.type,
            "times_json": r.times_json,
            "target_mode": r.target_mode,
            "is_enabled": r.is_enabled,
        } for r in rules])


@scheduler_api.route('/schedule/rules/<int:account_id>', methods=['GET'])
def list_schedule_rules(account_id):
    with get_db_context() as db:
        rules = db.query(ScheduleRule).filter(ScheduleRule.account_id == account_id).all()
        return jsonify([{
            "id": r.id,
            "account_id": r.account_id,
            "type": r.type,
            "times_json": r.times_json,
            "target_mode": r.target_mode,
            "selected_target_ids_json": r.selected_target_ids_json,
            "is_enabled": r.is_enabled,
        } for r in rules])


@scheduler_api.route('/schedule/rules/<int:account_id>', methods=['POST'])
def create_schedule_rule(account_id):
    data = request.get_json() or {}
    if not data.get("type"):
        return jsonify({"error": "type (PROMO or INFO) required"}), 400
    times = data.get("times_json", ["10:00", "18:00"])
    if isinstance(times, list):
        times = json.dumps(times)
    with get_db_context() as db:
        r = ScheduleRule(
            account_id=account_id,
            type=data["type"],
            times_json=times,
            target_mode=data.get("target_mode", "ALL_BOUND"),
            selected_target_ids_json=json.dumps(data.get("selected_target_ids", [])) if data.get("selected_target_ids") else None,
            is_enabled=data.get("is_enabled", True)
        )
        db.add(r)
        db.flush()
        return jsonify({"id": r.id, "success": True})


@scheduler_api.route('/schedule/rules/<int:account_id>/<int:rule_id>', methods=['DELETE'])
def delete_schedule_rule(account_id, rule_id):
    with get_db_context() as db:
        r = db.query(ScheduleRule).filter(
            ScheduleRule.id == rule_id,
            ScheduleRule.account_id == account_id
        ).first()
        if not r:
            return jsonify({"error": "Rule not found"}), 404
        db.delete(r)
        return jsonify({"success": True})


# ============ Bulk Apply ============
@scheduler_api.route('/schedule/bulk-apply', methods=['POST'])
def bulk_apply_schedule():
    """Copy profile + rules + template (+ bindings) from one account to many."""
    data = request.get_json() or {}
    source_id = data.get('source_account_id')
    target_ids = data.get('target_account_ids', [])
    copy_profile = data.get('copy_profile', True)
    copy_rules = data.get('copy_rules', True)
    copy_template = data.get('copy_template', True)
    copy_bindings = data.get('copy_bindings', True)

    if not source_id:
        return jsonify({'error': 'source_account_id required'}), 400
    if not target_ids or not isinstance(target_ids, list):
        return jsonify({'error': 'target_account_ids must be a non-empty list'}), 400
    if len(target_ids) > 50:
        return jsonify({'error': 'Maximum 50 target accounts per request'}), 400
    target_ids = [tid for tid in target_ids if tid != source_id]

    # Load source data into plain dicts (session-independent)
    profile_data = rules_data = templates_data = bindings_data = None
    with get_db_context() as db:
        if copy_profile:
            src = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == source_id).first()
            if not src:
                return jsonify({'error': 'Source account has no schedule profile. Save settings first.'}), 400
            profile_data = {
                'is_enabled': src.is_enabled, 'timezone': src.timezone,
                'min_interval_sec': src.min_interval_sec, 'daily_cap_total': src.daily_cap_total,
                'daily_cap_promo': src.daily_cap_promo, 'daily_cap_info': src.daily_cap_info,
                'jitter_sec': src.jitter_sec, 'quiet_hours_json': src.quiet_hours_json,
            }
        if copy_rules:
            rules_data = [
                {'type': r.type, 'times_json': r.times_json, 'target_mode': r.target_mode,
                 'selected_target_ids_json': r.selected_target_ids_json, 'is_enabled': r.is_enabled}
                for r in db.query(ScheduleRule).filter(ScheduleRule.account_id == source_id).all()
            ]
        if copy_template:
            templates_data = [
                {'type': t.type, 'name': t.name, 'body': t.body,
                 'is_active': t.is_active, 'weight': t.weight}
                for t in db.query(MessageTemplate).filter(
                    MessageTemplate.account_id == source_id,
                    MessageTemplate.scope == 'ACCOUNT'
                ).all()
            ]
        if copy_bindings:
            bindings_data = []
            for b in db.query(AccountTargetBinding).filter(
                AccountTargetBinding.account_id == source_id
            ).all():
                ct = db.query(ChatTarget).filter(ChatTarget.id == b.target_id).first()
                if ct:
                    ich = classify_target(ct).get("health")
                    if ich == HEALTH_INVALID or ich == HEALTH_NEEDS_REPAIR:
                        continue
                bindings_data.append({
                    'target_id': b.target_id, 'can_post': b.can_post,
                    'allowed_types': b.allowed_types, 'daily_cap': b.daily_cap,
                })

    results = []
    for target_id in target_ids:
        try:
            with get_db_context() as db:
                if profile_data:
                    existing = db.query(ScheduleProfile).filter(
                        ScheduleProfile.account_id == target_id
                    ).first()
                    if existing:
                        for k, v in profile_data.items():
                            setattr(existing, k, v)
                    else:
                        db.add(ScheduleProfile(account_id=target_id, **profile_data))

                if rules_data is not None:
                    for r in db.query(ScheduleRule).filter(
                        ScheduleRule.account_id == target_id
                    ).all():
                        db.delete(r)
                    db.flush()
                    for rd in rules_data:
                        db.add(ScheduleRule(account_id=target_id, **rd))

                if templates_data is not None:
                    for t in db.query(MessageTemplate).filter(
                        MessageTemplate.account_id == target_id,
                        MessageTemplate.scope == 'ACCOUNT'
                    ).all():
                        db.delete(t)
                    db.flush()
                    for td in templates_data:
                        db.add(MessageTemplate(scope='ACCOUNT', account_id=target_id, **td))

                if bindings_data is not None:
                    existing_tids = {b.target_id for b in db.query(AccountTargetBinding).filter(
                        AccountTargetBinding.account_id == target_id
                    ).all()}
                    for bd in bindings_data:
                        if bd['target_id'] not in existing_tids:
                            db.add(AccountTargetBinding(account_id=target_id, **bd))

            results.append({'account_id': target_id, 'success': True})
        except Exception as e:
            results.append({'account_id': target_id, 'success': False, 'error': str(e)})

    succeeded = sum(1 for r in results if r['success'])
    return jsonify({
        'results': results, 'total': len(results),
        'succeeded': succeeded, 'failed': len(results) - succeeded,
    })


# ============ Run Now (Test) ============
@scheduler_api.route('/jobs/run-now', methods=['POST'])
def run_job_now():
    """Create and execute a job immediately for testing."""
    data = request.get_json() or {}
    account_id = data.get("account_id")
    target_id = data.get("target_id")
    msg_type = data.get("type")
    if not all([account_id, target_id, msg_type]):
        return jsonify({"error": "account_id, target_id, type required"}), 400
    if msg_type not in ("PROMO", "INFO"):
        return jsonify({"error": "type must be PROMO or INFO"}), 400
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == int(account_id)).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        if account.status != AccountStatus.ACTIVE:
            return jsonify({
                "error": "Account is not active — re-login or enable the account before sending",
            }), 400
        target = db.query(ChatTarget).filter(ChatTarget.id == int(target_id)).first()
        if not target:
            return jsonify({"error": "Target not found"}), 404
        th = classify_target(target)
        if th.get("health") == HEALTH_INVALID:
            return jsonify({
                "error": th.get("reason") or "Target is not usable for scheduler sends",
                "health": HEALTH_INVALID,
            }), 400
        if th.get("health") == HEALTH_NEEDS_REPAIR:
            return jsonify({
                "error": th.get("reason") or "Target needs @username or invite before sending",
                "health": HEALTH_NEEDS_REPAIR,
            }), 400
        mh = merged_target_health_row(db, target, int(account_id))
        if not is_health_allowed_for_send(mh["health"]):
            return jsonify({
                "error": mh.get("health_reason")
                or "Target is not sendable for this account (recent failures or permissions)",
                "health": mh.get("health"),
            }), 400
        binding = db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == account_id,
            AccountTargetBinding.target_id == target_id
        ).first()
        if not binding:
            return jsonify({"error": "No binding for this account-target pair"}), 400
        if not binding.can_post:
            return jsonify({
                "error": "Binding has no post permission — resolve join/permissions or remove this target",
            }), 400
        pace = get_send_pacing_decision(
            db,
            int(account_id),
            int(target_id),
            is_test=True,
            binding_created_at=getattr(binding, "created_at", None),
        )
        if not pace.get("allowed"):
            return jsonify({
                "error": "Pacing guard: wait before the next send for this account (and target spacing).",
                "pacing_blocked": True,
                "retry_after_sec": int(pace.get("retry_after_sec") or 0),
                "next_allowed_at": pace.get("next_allowed_at"),
                "reasons": pace.get("reasons") or [],
            }), 409
        from src.dashboard.scheduler_mutations import is_scoped_campaign_pilot_run_now_allowed

        job_marker = SCHEDULED_JOB_OPERATOR_SEND_TEST_MARKER
        if is_scoped_campaign_pilot_run_now_allowed(int(account_id), int(target_id)):
            from src.scheduler.campaign_governance import check_governed_campaign_send_allowed

            allowed, block_reason = check_governed_campaign_send_allowed(
                db, int(account_id), int(target_id), job_marker=SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER
            )
            if not allowed:
                return jsonify(
                    {
                        "error": block_reason or "campaign_execution_blocked",
                        "code": "campaign_execution_blocked",
                        "campaign_execution_enabled": False,
                    }
                ), 423
            job_marker = SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER
        job = ScheduledJob(
            account_id=int(account_id),
            target_id=int(target_id),
            type=msg_type,
            run_at=utc_now_naive(),
            status=JobStatus.PENDING,
            last_error=job_marker,
        )
        db.add(job)
        db.flush()
        job_id = job.id
    # Telethon runs in autostory-scheduler only — avoids Gunicorn event-loop / SQLite session fights.
    return jsonify({
        "queued": True,
        "job_id": job_id,
        "message": "Send test queued — watch Last deliveries",
    })


# ============ Deliveries (Logs) ============
@scheduler_api.route('/deliveries', methods=['GET'])
def list_deliveries():
    account_id = request.args.get("account_id", type=int)
    target_id = request.args.get("target_id", type=int)
    msg_type = request.args.get("type")
    limit = request.args.get("limit", 100, type=int) or 100
    limit = max(1, min(int(limit), 200))
    # Optional rolling window (UTC naive, same basis as ``MessageDelivery.created_at``).
    days = request.args.get("days", type=float)
    if days is not None:
        days = max(0.0, min(float(days), 90.0))
    with get_db_context() as db:
        q = db.query(MessageDelivery).order_by(MessageDelivery.created_at.desc())
        if account_id:
            q = q.filter(MessageDelivery.account_id == account_id)
        if target_id:
            q = q.filter(MessageDelivery.target_id == target_id)
        if msg_type:
            q = q.filter(MessageDelivery.type == msg_type)
        if days is not None and days > 0:
            cutoff = datetime.utcnow() - timedelta(days=days)
            q = q.filter(MessageDelivery.created_at >= cutoff)
        deliveries = q.limit(limit).all()
        out = []
        for d in deliveries:
            st = d.status
            if hasattr(st, "value"):
                st = st.value
            out.append({
                "id": d.id,
                "account_id": d.account_id,
                "target_id": d.target_id,
                "type": d.type,
                "status": st,
                "sent_at": to_utc_iso_z(d.sent_at),
                "error_code": getattr(d, "error_code", None),
                "error_message": d.error_message,
                "created_at": to_utc_iso_z(d.created_at),
            })
        return jsonify(out)
