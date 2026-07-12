"""
P6 — Operator control web routes (read-mostly; safe mutations only).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from flask_wtf.csrf import validate_csrf
from wtforms import ValidationError

from src.core.database import get_db_context
from src.core.scheduler_models import JobStatus, ScheduledJob
from src.dashboard.auth_access import dashboard_api_authorized
from src.dashboard.operator_control_service import (
    build_account_detail,
    build_account_inventory_row,
    build_schedule_eligibility_rows,
    build_system_safety_snapshot,
    build_target_detail,
    list_bindings,
    list_deliveries_fleet,
    list_gateway_jobs,
    list_scheduled_jobs,
    list_targets,
    queue_counts_snapshot,
)
from src.scheduler.generation_eligibility import evaluate_generation_eligibility

operator_control_bp = Blueprint("operator_control", __name__)


def _require_api_access():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return None


def _csrf_ok() -> bool:
    if not current_app.config.get("WTF_CSRF_ENABLED", True):
        return True
    try:
        validate_csrf(request.form.get("csrf_token") or request.headers.get("X-CSRFToken"))
        return True
    except ValidationError:
        return False


@operator_control_bp.route("/operator", methods=["GET"])
@login_required
def operator_hub():
    return render_template("operator/hub.html")


@operator_control_bp.route("/operator/accounts", methods=["GET"])
@login_required
def operator_accounts():
    with get_db_context() as db:
        from src.core.models import Account

        rows = [build_account_inventory_row(db, a) for a in db.query(Account).order_by(Account.id.asc()).all()]
    return render_template("operator/accounts.html", accounts=rows)


@operator_control_bp.route("/operator/accounts/<int:account_id>", methods=["GET"])
@login_required
def operator_account_detail(account_id: int):
    with get_db_context() as db:
        detail = build_account_detail(db, account_id)
    if detail is None:
        return render_template("operator/not_found.html", kind="account", item_id=account_id), 404
    return render_template("operator/account_detail.html", detail=detail)


@operator_control_bp.route("/operator/accounts/<int:account_id>/refresh-readiness", methods=["POST"])
@login_required
def operator_refresh_readiness(account_id: int):
    if not _csrf_ok():
        flash("CSRF validation failed.", "danger")
        return redirect(url_for("operator_control.operator_account_detail", account_id=account_id))
    from src.clients.readiness_worker import _deep_check_one

    try:
        asyncio.run(asyncio.wait_for(_deep_check_one(int(account_id)), timeout=25.0))
        flash(f"Readiness probe completed for account {account_id}.", "success")
    except Exception as exc:
        flash(f"Readiness probe failed: {exc.__class__.__name__}", "warning")
    return redirect(url_for("operator_control.operator_account_detail", account_id=account_id))


@operator_control_bp.route("/operator/targets", methods=["GET"])
@login_required
def operator_targets():
    with get_db_context() as db:
        targets = list_targets(db)
    return render_template("operator/targets.html", targets=targets)


@operator_control_bp.route("/operator/targets/<int:target_id>", methods=["GET"])
@login_required
def operator_target_detail(target_id: int):
    with get_db_context() as db:
        detail = build_target_detail(db, target_id)
    if detail is None:
        return render_template("operator/not_found.html", kind="target", item_id=target_id), 404
    return render_template("operator/target_detail.html", detail=detail)


@operator_control_bp.route("/operator/bindings", methods=["GET"])
@login_required
def operator_bindings():
    with get_db_context() as db:
        bindings = list_bindings(db)
    return render_template("operator/bindings.html", bindings=bindings)


@operator_control_bp.route("/operator/schedules", methods=["GET"])
@login_required
def operator_schedules():
    with get_db_context() as db:
        rows = build_schedule_eligibility_rows(db)
    return render_template("operator/schedules.html", schedules=rows)


@operator_control_bp.route("/operator/jobs", methods=["GET"])
@login_required
def operator_jobs():
    account_id = request.args.get("account_id", type=int)
    status = request.args.get("status")
    with get_db_context() as db:
        jobs = list_scheduled_jobs(db, account_id=account_id, status=status)
    return render_template("operator/jobs.html", jobs=jobs, account_id=account_id, status=status)


@operator_control_bp.route("/operator/jobs/<int:job_id>/cancel", methods=["POST"])
@login_required
def operator_cancel_job(job_id: int):
    if not _csrf_ok():
        flash("CSRF validation failed.", "danger")
        return redirect(url_for("operator_control.operator_jobs"))
    with get_db_context() as db:
        job = db.get(ScheduledJob, int(job_id)) if hasattr(db, "get") else (
            db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        )
        if job is None:
            flash(f"Job {job_id} not found.", "warning")
            return redirect(url_for("operator_control.operator_jobs"))
        st = str(job.status or "").upper()
        if st != JobStatus.PENDING.value:
            flash(f"Job {job_id} is {st}; only PENDING jobs can be cancelled.", "danger")
            return redirect(url_for("operator_control.operator_jobs", account_id=job.account_id))
        job.status = JobStatus.CANCELLED.value
        db.commit()
        flash(f"Cancelled pending job {job_id}.", "success")
    return redirect(url_for("operator_control.operator_jobs"))


@operator_control_bp.route("/operator/gateway", methods=["GET"])
@login_required
def operator_gateway():
    account_id = request.args.get("account_id", type=int)
    with get_db_context() as db:
        jobs = list_gateway_jobs(db, account_id=account_id)
    return render_template("operator/gateway.html", jobs=jobs, account_id=account_id)


@operator_control_bp.route("/operator/deliveries", methods=["GET"])
@login_required
def operator_deliveries():
    account_id = request.args.get("account_id", type=int)
    with get_db_context() as db:
        deliveries = list_deliveries_fleet(db, account_id=account_id)
    return render_template("operator/deliveries.html", deliveries=deliveries, account_id=account_id)


@operator_control_bp.route("/operator/system-safety", methods=["GET"])
@login_required
def operator_system_safety():
    with get_db_context() as db:
        snapshot = build_system_safety_snapshot(db)
    return render_template("operator/system_safety.html", snapshot=snapshot)


@operator_control_bp.route("/api/operator/eligibility-preview", methods=["GET"])
def api_operator_eligibility_preview():
    denied = _require_api_access()
    if denied is not None:
        return denied
    account_id = request.args.get("account_id", type=int)
    target_id = request.args.get("target_id", type=int)
    job_type = (request.args.get("job_type") or "PROMO").strip().upper()
    if account_id is None or target_id is None:
        return jsonify({"error": "account_id and target_id required"}), 400
    with get_db_context() as db:
        before = queue_counts_snapshot(db)
        decision = evaluate_generation_eligibility(
            db,
            job_type=job_type,
            account_id=int(account_id),
            target_id=int(target_id),
            generation_scope="normal",
        )
        after = queue_counts_snapshot(db)
    return jsonify(
        {
            "preview": decision.to_dict(),
            "non_mutating_proof": {
                "before": before,
                "after": after,
                "unchanged": before == after,
            },
        }
    )


@operator_control_bp.route("/api/operator/queue-snapshot", methods=["GET"])
def api_operator_queue_snapshot():
    denied = _require_api_access()
    if denied is not None:
        return denied
    with get_db_context() as db:
        snap = queue_counts_snapshot(db)
    return jsonify(snap)
