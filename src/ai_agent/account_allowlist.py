"""
Optional allowlist: Telegram accounts that may be used for AI Agent I/O.

When ``AI_AGENT_ACCOUNT_PHONES`` / ``AI_AGENT_ACCOUNT_IDS`` (via settings) are
non-empty, only those accounts are valid for AI Agent create/send/fetch paths.
Scheduler and other subsystems treat the same set as *reserved* for AI Agent.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from config.settings import settings
from src.core.models import Account

logger = structlog.get_logger(__name__)

# Production AI Telegram lines — never attach readiness / scheduler story workers here.
RESERVED_AI_AGENT_ACCOUNT_IDS: frozenset[int] = frozenset({110, 113, 131})

_account_skipped_logged: set[int] = set()
_account_skipped_lock = threading.Lock()


def _split_csv(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def normalize_phone_for_match(phone: str) -> str:
    """Normalize phone strings for comparison with DB ``accounts.phone_number``."""
    raw = (phone or "").strip().replace(" ", "").replace("-", "")
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    return "+" + digits


def ai_agent_allowlist_configured() -> bool:
    phones = _split_csv(settings.ai_agent_account_phones)
    ids = _split_csv(settings.ai_agent_account_ids)
    return bool(phones or ids)


def resolve_ai_agent_task_permitted_account_ids(db: Session) -> frozenset[int]:
    """
    Accounts allowed for AI Agent tasks (dropdown + create + send/fetch gate).

    Union of: fixed production pool ``110/113/131``, env allowlist
    (``AI_AGENT_ACCOUNT_IDS`` / ``AI_AGENT_ACCOUNT_PHONES``), and any account with
    ``purpose='ai_agent'``.
    """
    allowed: set[int] = set(RESERVED_AI_AGENT_ACCOUNT_IDS)
    allowed |= set(resolve_ai_agent_allowed_account_ids(db))
    for r in (
        db.query(Account.id)
        .filter(func.lower(func.coalesce(Account.purpose, "")) == "ai_agent")
        .all()
    ):
        allowed.add(int(r[0]))
    return frozenset(allowed)


def account_id_permitted_for_ai_agent_tasks(db: Session, account_id: int) -> bool:
    return int(account_id) in resolve_ai_agent_task_permitted_account_ids(db)


def resolve_ai_agent_allowed_account_ids(db: Session) -> frozenset[int]:
    """
    Union of explicit ids from env and accounts whose phone matches configured phones.
    """
    allowed: set[int] = set()
    for part in _split_csv(settings.ai_agent_account_ids):
        try:
            allowed.add(int(part))
        except ValueError:
            continue
    phones = {normalize_phone_for_match(p) for p in _split_csv(settings.ai_agent_account_phones)}
    phones.discard("")
    if phones:
        for aid, ph in db.query(Account.id, Account.phone_number).all():
            if ph is None:
                continue
            if normalize_phone_for_match(str(ph)) in phones:
                allowed.add(int(aid))
    return frozenset(allowed)


def account_id_is_ai_agent_allowed(db: Session, account_id: int) -> bool:
    """When allowlist is unset, every account is allowed (legacy behaviour)."""
    if not ai_agent_allowlist_configured():
        return True
    return int(account_id) in resolve_ai_agent_allowed_account_ids(db)


def annotate_account_api_rows_ai_reserved(db: Session, entries: list[dict]) -> None:
    """Mutates API account dicts with ``ai_agent_reserved`` (scheduler hides these when true)."""
    if not ai_agent_allowlist_configured():
        for e in entries:
            e["ai_agent_reserved"] = False
        return
    allowed = resolve_ai_agent_allowed_account_ids(db)
    for e in entries:
        try:
            e["ai_agent_reserved"] = int(e.get("id") or 0) in allowed
        except (TypeError, ValueError):
            e["ai_agent_reserved"] = False


def account_id_excluded_from_readiness_worker_core(account_id: int) -> bool:
    """Session isolation: fixed AI pool IDs skip readiness Telethon entirely."""
    return int(account_id) in RESERVED_AI_AGENT_ACCOUNT_IDS


def account_id_excluded_from_scheduler_worker(db: Session, account_id: int) -> bool:
    """
    Scheduler / story rotation must not use: core AI IDs, ``purpose='ai_agent'``,
    or accounts in ``AI_AGENT_ACCOUNT_IDS`` / ``AI_AGENT_ACCOUNT_PHONES`` allowlist.
    """
    aid = int(account_id)
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return True
    row = db.query(Account.purpose).filter(Account.id == aid).first()
    if row is not None:
        p = (row[0] or "").strip().lower()
        if p == "ai_agent":
            return True
    if ai_agent_allowlist_configured() and aid in resolve_ai_agent_allowed_account_ids(db):
        return True
    return False


def scheduler_telethon_excluded_account_ids(db: Session) -> frozenset[int]:
    """IDs for ``NOT IN`` in story rotation queries (core pool + ai_agent purpose + allowlist)."""
    out: set[int] = set(RESERVED_AI_AGENT_ACCOUNT_IDS)
    if ai_agent_allowlist_configured():
        out |= set(resolve_ai_agent_allowed_account_ids(db))
    for r in (
        db.query(Account.id)
        .filter(func.lower(func.coalesce(Account.purpose, "")) == "ai_agent")
        .all()
    ):
        out.add(int(r[0]))
    return frozenset(out)


def gateway_claim_allowed_account_ids(db: Session) -> Optional[frozenset[int]]:
    """
    Account IDs the gateway worker may claim from the queue.

    - If the AI allowlist is configured, use its resolved IDs.
    - Else if ``TELEGRAM_GATEWAY_ENFORCE_DEFAULT_AI_IDS`` is truthy, only
      ``110, 113, 131`` (production AI pool).

    Returns ``None`` when no restriction (legacy / dev).
    """
    if ai_agent_allowlist_configured():
        allowed = set(resolve_ai_agent_allowed_account_ids(db))
        try:
            from src.core.p5c_authorization import p5c_gateway_claim_extra_account_ids
            from src.core.p5d_authorization import p5d_gateway_claim_extra_account_ids
            from src.core.p6_4_authorization import p6_4_gateway_claim_extra_account_ids

            allowed |= set(p5c_gateway_claim_extra_account_ids())
            allowed |= set(p5d_gateway_claim_extra_account_ids())
            allowed |= set(p6_4_gateway_claim_extra_account_ids())
        except Exception:
            pass
        return frozenset(allowed)
    raw = os.environ.get("TELEGRAM_GATEWAY_ENFORCE_DEFAULT_AI_IDS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return frozenset({110, 113, 131})
    return None


def ai_agent_inbound_fetch_allowed_account_ids(db: Session) -> Optional[frozenset[int]]:
    """
    Same rule as ``gateway_claim_allowed_account_ids``: inbound Telegram fetch/sync
    must match the gateway worker claim filter when a restriction is active.
    """
    return gateway_claim_allowed_account_ids(db)


def account_id_may_ai_agent_inbound_fetch(db: Session, account_id: int) -> bool:
    allowed = ai_agent_inbound_fetch_allowed_account_ids(db)
    if allowed is None:
        return True
    return int(account_id) in allowed


def log_ai_agent_account_skipped_once(account_id: int, *, reason: str) -> None:
    """Emit ``ai_agent_account_skipped`` at most once per account_id (per process)."""
    aid = int(account_id)
    with _account_skipped_lock:
        if aid in _account_skipped_logged:
            return
        _account_skipped_logged.add(aid)
    logger.info("ai_agent_account_skipped", account_id=aid, reason=reason)


def log_ai_agent_inbound_allowlist_at_startup() -> None:
    """Debug: resolved inbound-allowed account IDs (same as gateway claim)."""
    try:
        from src.core.database import get_db_context

        with get_db_context() as db:
            ids = ai_agent_inbound_fetch_allowed_account_ids(db)
        if ids is None:
            logger.debug("ai_agent_inbound_allowed_account_ids", unrestricted=True)
        else:
            logger.debug(
                "ai_agent_inbound_allowed_account_ids",
                unrestricted=False,
                allowed_account_ids=sorted(ids),
            )
    except Exception as e:
        logger.warning("ai_agent_inbound_allowlist_startup_log_failed", error=str(e))


def account_id_is_ai_agent_reserved(db: Session, account_id: int) -> bool:
    """
    True when this account is in the configured AI Agent pool (scheduler must avoid it).
    When allowlist is unset, no account is considered reserved.
    """
    if not ai_agent_allowlist_configured():
        return False
    return int(account_id) in resolve_ai_agent_allowed_account_ids(db)
