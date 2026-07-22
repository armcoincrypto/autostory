"""Seed minimal fleet accounts/targets for DATABASE_STATE_DEPENDENCY tests.

Does not copy production secrets or real Telegram sessions. Session files are
empty placeholders so ``account_has_canonical_session`` / file probes succeed
while Telegram I/O stays mocked in tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from src.core.models import Account, AccountStatus, DiscoveredUser
from src.core.scheduler_models import AccountReadinessSnapshot, ChatTarget
from src.core.session_paths import get_canonical_session_path, get_sessions_dir

# Stable test fleet IDs referenced by P6 / P10 suites.
FLEET_ACCOUNT_IDS: tuple[int, ...] = (107, 110, 139, 140)
BINANCE_ARMENIA_SOURCE_ID = 1936532075
DEFAULT_TARGET_ID = 14

# Unique phones — never reuse production numbers.
_PHONE_BY_ID = {
    107: "+15550000107",
    110: "+15550000110",
    139: "+15550000139",
    140: "+15550000140",
}


def _ensure_dummy_session_file(account_id: int) -> None:
    """Write a minimal Telethon schema-v8 SQLite session (no real auth key secrets)."""
    import sqlite3

    sessions_dir = get_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = get_canonical_session_path(int(account_id))
    # Always (re)write a compatible placeholder so empty/corrupt leftovers do not
    # trip legacy_sqlite_session_format in resolver-backed eligibility checks.
    if path.is_file():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE version (version INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO version VALUES (8)")
        con.execute(
            """CREATE TABLE sessions (
            dc_id INTEGER PRIMARY KEY,
            server_address TEXT, port INTEGER,
            auth_key BLOB, takeout_id INTEGER, tmp_auth_key BLOB)"""
        )
        # Dummy non-secret auth_key blob — tests mock Telegram I/O.
        con.execute(
            "INSERT INTO sessions VALUES (1, '127.0.0.1', 443, x'00', NULL, NULL)"
        )
        con.commit()
    finally:
        con.close()


def _upsert_account(db, *, account_id: int, purpose: str, health_status: str | None) -> Account:
    phone = _PHONE_BY_ID[int(account_id)]
    aged = datetime.utcnow() - timedelta(days=7)
    acc = db.query(Account).filter(Account.id == int(account_id)).first()
    if acc is None:
        # Avoid unique phone collisions if a different row already holds the phone.
        existing_phone = db.query(Account).filter(Account.phone_number == phone).first()
        if existing_phone is not None and int(existing_phone.id) != int(account_id):
            existing_phone.phone_number = f"+15559{int(account_id):07d}"
            db.flush()
        acc = Account(
            id=int(account_id),
            phone_number=phone,
            status=AccountStatus.ACTIVE,
            purpose=purpose,
            health_status=health_status,
            session_string=None,
            stories_today=0,
            created_at=aged,
        )
        db.add(acc)
    else:
        acc.phone_number = phone
        acc.status = AccountStatus.ACTIVE
        acc.purpose = purpose
        if health_status is not None:
            acc.health_status = health_status
        acc.stories_today = int(getattr(acc, "stories_today", 0) or 0)
        # Age the row so warmup gates do not block dry-run story precheck.
        if getattr(acc, "created_at", None) is None or acc.created_at > aged:
            acc.created_at = aged
        # Never persist real secrets from production into the work DB.
        if acc.session_string and len(str(acc.session_string)) > 80:
            acc.session_string = None
    if hasattr(acc, "imported_at"):
        acc.imported_at = aged
    if hasattr(acc, "warmup_status"):
        acc.warmup_status = "warmed"
    _ensure_dummy_session_file(account_id)
    return acc


def _upsert_readiness(
    db,
    account_id: int,
    *,
    status: str,
    reason: str | None = None,
    failure_code: str | None = None,
    hours_valid: float = 6.0,
) -> None:
    now = datetime.utcnow()
    row = (
        db.query(AccountReadinessSnapshot)
        .filter(AccountReadinessSnapshot.account_id == int(account_id))
        .first()
    )
    if row is None:
        row = AccountReadinessSnapshot(account_id=int(account_id))
        db.add(row)
    row.status = status
    row.reason = reason
    row.failure_code = failure_code
    row.checked_at = now
    row.expires_at = now + timedelta(hours=hours_valid)


def _seed_discovered_users(db, *, source_chat_id: int = BINANCE_ARMENIA_SOURCE_ID, count: int = 8) -> None:
    existing = (
        db.query(DiscoveredUser)
        .filter(DiscoveredUser.source_chat_id == int(source_chat_id))
        .count()
    )
    if existing >= count:
        return
    need = count - existing
    # High synthetic ids to avoid colliding with any imported production users.
    base = 9_100_000_000 + int(source_chat_id) % 10_000
    for i in range(need):
        uid = base + existing + i + 1
        if db.query(DiscoveredUser).filter(DiscoveredUser.user_id == uid).first():
            continue
        db.add(
            DiscoveredUser(
                user_id=uid,
                username=f"seed_user_{uid}",
                first_name="Seed",
                last_name=str(uid),
                source_chat_id=int(source_chat_id),
                source_chat_title="seed_binance_armenia",
                is_blocked=False,
                times_mentioned=0,
            )
        )


def _seed_chat_target(db, target_id: int = DEFAULT_TARGET_ID) -> ChatTarget:
    target = db.query(ChatTarget).filter(ChatTarget.id == int(target_id)).first()
    if target is None:
        target = ChatTarget(
            id=int(target_id),
            tg_id=-(1_000_000_000 + int(target_id)),
            username=f"seed_target_{target_id}",
            title=f"Seed target {target_id}",
            chat_type="channel",
            is_verified=True,
        )
        db.add(target)
    else:
        if not (target.username or target.invite_link):
            target.username = f"seed_target_{target_id}"
        if not target.chat_type:
            target.chat_type = "channel"
    return target


def seed_minimal_fleet(
    db,
    *,
    account_ids: Iterable[int] | None = None,
    seed_mentions: bool = True,
    seed_target: bool = True,
) -> dict:
    """
    Upsert Accounts 107/110/139/140 (or a subset) plus mention users / target 14.

    Returns a small summary dict for assertions/debugging.
    """
    ids = tuple(int(x) for x in (account_ids or FLEET_ACCOUNT_IDS))
    for aid in ids:
        if aid == 110:
            # Protected + AI-reserved in product constants; keep purpose ai_agent.
            _upsert_account(db, account_id=110, purpose="ai_agent", health_status="alive")
            _upsert_readiness(db, 110, status="READY", reason="seed_reserved")
        elif aid == 139:
            _upsert_account(db, account_id=139, purpose="both", health_status="error")
            _upsert_readiness(
                db,
                139,
                status="NOT_AUTHORIZED",
                reason="seed_not_authorized",
                failure_code="unauthorized_session",
            )
        elif aid == 140:
            _upsert_account(db, account_id=140, purpose="both", health_status="alive")
            _upsert_readiness(db, 140, status="READY", reason="seed_story_ready")
            acc = db.query(Account).filter(Account.id == 140).first()
            if acc is not None:
                acc.story_precheck_status = "allowed"
                acc.story_precheck_checked_at = datetime.utcnow()
                acc.stories_today = 0
        elif aid == 107:
            _upsert_account(db, account_id=107, purpose="both", health_status="alive")
            _upsert_readiness(db, 107, status="READY", reason="seed_fleet")
        else:
            _upsert_account(db, account_id=aid, purpose="both", health_status="alive")
            _upsert_readiness(db, aid, status="READY", reason="seed_generic")

    if seed_mentions:
        _seed_discovered_users(db)
    if seed_target:
        _seed_chat_target(db)

    db.commit()
    return {
        "account_ids": list(ids),
        "mention_source_chat_id": BINANCE_ARMENIA_SOURCE_ID if seed_mentions else None,
        "target_id": DEFAULT_TARGET_ID if seed_target else None,
    }
