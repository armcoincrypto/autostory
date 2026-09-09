"""
Database configuration and session management
"""
import os
import importlib
import random
import sys
import sqlite3
import time
from contextlib import contextmanager
from typing import Callable, Generator, TypeVar

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session, declarative_base
import structlog

# Add project root to path
_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_here, "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from config.settings import settings

logger = structlog.get_logger(__name__)

# Create base class for models
Base = declarative_base()

# SQLite: WAL and busy_timeout (ms)
SQLITE_BUSY_TIMEOUT_MS = 30000
SQLITE_CONNECT_TIMEOUT_SEC = 60


def _is_sqlite(url: str) -> bool:
    """True if url is a SQLite database URL."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip().split("?")[0]
    return u.startswith("sqlite://") or u.lower().startswith("sqlite:")


def _sqlite_path_from_url(url: str) -> str:
    """Resolve SQLite URL to filesystem path or :memory:."""
    u = (url or "").strip().split("?")[0]
    if not u.startswith("sqlite://"):
        return ":memory:"
    # sqlite:///rel  or  sqlite:////absolute  or  sqlite:///:memory:
    rest = u[9:]  # after "sqlite://"
    if not rest or ":memory:" in rest:
        return ":memory:"
    if rest.startswith("//"):
        path = rest[1:]  # absolute: //opt/... -> /opt/...
    else:
        path = rest  # e.g. data/storyfleet.db or /./data/storyfleet.db
    if not path or path == ":memory:":
        return ":memory:"
    # ./data/foo.db or /./data/foo.db are relative to project root (sqlite:///./ convention)
    if path.startswith("/./"):
        path = path[3:]
    elif path.startswith("./"):
        path = path[2:]
    if not os.path.isabs(path):
        path = os.path.join(_project_root, path)
    return os.path.abspath(path)


def _sqlite_creator() -> sqlite3.Connection:
    """Create a SQLite connection with WAL and busy_timeout set at creation (no event reliance)."""
    url = settings.database.url
    path = _sqlite_path_from_url(url)
    if path != ":memory:":
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(
        path,
        timeout=SQLITE_CONNECT_TIMEOUT_SEC,
        check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    # Reduce fsync pressure vs FULL while keeping WAL durability acceptable for dashboard use.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _make_engine():
    url = settings.database.url
    kwargs = {
        "echo": settings.environment == "development",
    }
    if _is_sqlite(url):
        # Use creator so every connection gets WAL + busy_timeout at open (active in all entrypoints)
        kwargs["creator"] = _sqlite_creator
        if _sqlite_path_from_url(url) == ":memory:":
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        else:
            kwargs["pool_size"] = 2
            kwargs["max_overflow"] = 4
            kwargs["pool_pre_ping"] = True
    else:
        kwargs["pool_size"] = settings.database.pool_size
        kwargs["max_overflow"] = settings.database.max_overflow
        kwargs["pool_pre_ping"] = True

    return create_engine(url, **kwargs)


# Create engine (single source of truth for all processes: web, scheduler, bot)
engine = _make_engine()


def _apply_sqlite_pragma_busy(dbapi_connection) -> None:
    """Ensure WAL + busy_timeout on every SQLite connection (belt-and-suspenders vs pool/creator edge cases)."""
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:
        logger.warning("sqlite_pragma_apply_failed", exc_info=True)


@event.listens_for(engine, "connect")
def _on_engine_connect(dbapi_connection, connection_record):
    if engine.dialect.name == "sqlite":
        _apply_sqlite_pragma_busy(dbapi_connection)


T = TypeVar("T")


def _sqlite_transient_lock_error(exc: BaseException) -> bool:
    from sqlalchemy.exc import OperationalError

    if not isinstance(exc, OperationalError):
        return False
    orig = getattr(exc, "orig", None)
    msg = " ".join(
        str(x)
        for x in (exc, orig)
        if x is not None
    ).lower()
    return "database is locked" in msg or "database is busy" in msg


def run_with_sqlite_lock_retry(
    fn: Callable[[], T],
    *,
    operation: str,
    max_attempts: int | None = None,
    retry_log_event: str = "sqlite_lock_retry",
    **log_extra,
) -> T:
    """
    Retry a callable that performs one complete DB transaction (e.g. ``with get_db_context()``)
    when SQLite returns a transient lock/busy error. Non-lock errors are not retried.
    """
    from sqlalchemy.exc import OperationalError

    if not _is_sqlite(settings.database.url):
        return fn()

    attempts = max_attempts if max_attempts is not None else int(os.environ.get("SQLITE_WRITE_MAX_RETRIES", "8"))
    attempts = max(1, attempts)
    base = float(os.environ.get("SQLITE_LOCK_RETRY_BASE_SEC", "0.05"))
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except OperationalError as e:
            last_exc = e
            if attempt >= attempts - 1 or not _sqlite_transient_lock_error(e):
                raise
            delay = base * (2**attempt) + random.uniform(0, 0.08)
            logger.warning(
                retry_log_event,
                operation=operation,
                attempt=attempt + 1,
                max_attempts=attempts,
                sleep_sec=round(delay, 4),
                error_message=str(e)[:220],
                **log_extra,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc

# Create session factory (expire_on_commit=False so Account objects stay usable after session closes)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


def _import_optional_model_module(module_name: str) -> None:
    """Register optional feature models when their source is installed."""
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Only suppress absence of the requested optional module itself. A
        # missing dependency inside an installed module is a real startup bug.
        missing = str(exc.name or "")
        if missing != module_name and not module_name.startswith(f"{missing}."):
            raise
        logger.warning("optional_model_module_unavailable", module=module_name)


def init_db() -> None:
    """Initialize database tables"""
    from . import models  # noqa: F401
    import src.core.scheduler_models  # noqa: F401 - ChatTarget, membership probe cache, etc.
    import src.dashboard.models  # noqa: F401 - ensures dashboard_users table
    import src.telegram_gateway.models  # noqa: F401 - Telegram gateway job queue
    _import_optional_model_module("src.core.ai_agent_models")
    _import_optional_model_module("src.core.campaign_governance_models")
    _import_optional_model_module("src.governance.models")
    _import_optional_model_module("src.social_agent.models")
    _import_optional_model_module("src.messaging.models")
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    _ensure_account_governance_tables()
    _ensure_ai_agent_tasks_negotiation_stage_column()
    _ensure_ai_agent_tasks_auto_loop_columns()
    _ensure_accounts_purpose_column()
    _ensure_accounts_healthcheck_columns()
    _ensure_healthcheck_run_columns()
    _ensure_accounts_warmup_columns()
    _ensure_accounts_safety_columns()
    _ensure_accounts_identity_audit_columns()
    _ensure_accounts_profile_capability_columns()
    _ensure_account_risk_events_table()
    _ensure_discovered_users_source_username_column()
    _ensure_scheduled_jobs_lease_columns()
    _ensure_scheduled_jobs_dm_columns()
    _ensure_message_deliveries_send_intent_columns()
    _ensure_story_runs_mention_plan_column()
    _ensure_social_connections_meta_columns()
    _ensure_autostory_hardening_schema()
    logger.info("Database initialized", tables=list(Base.metadata.tables.keys()))
    if _is_sqlite(settings.database.url):
        try:
            with engine.connect() as conn:
                j = conn.execute(text("PRAGMA journal_mode")).scalar()
                b = conn.execute(text("PRAGMA busy_timeout")).scalar()
                logger.info("SQLite PRAGMA check", journal_mode=j, busy_timeout_ms=b)
                if j != "wal" or int(b) != SQLITE_BUSY_TIMEOUT_MS:
                    logger.warning(
                        "SQLite PRAGMA mismatch; expected journal_mode=wal, busy_timeout=%s",
                        SQLITE_BUSY_TIMEOUT_MS,
                        actual_journal_mode=j,
                        actual_busy_timeout=b,
                    )
        except Exception as e:
            logger.warning("SQLite PRAGMA check failed", error=str(e))


def _ensure_accounts_purpose_column() -> None:
    """Add purpose column to accounts if missing (for existing DBs)."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "accounts" not in insp.get_table_names():
        return
    cols = [c["name"] for c in insp.get_columns("accounts")]
    if "purpose" in cols:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE accounts ADD COLUMN purpose VARCHAR(20) DEFAULT 'both'"))
            conn.commit()
        logger.info("Added accounts.purpose column")
    except Exception as e:
        logger.warning("Could not add accounts.purpose column (may already exist)", error=str(e))


def _ensure_social_connections_meta_columns() -> None:
    """Additive Meta OAuth columns on social_connections for existing DBs."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "social_connections" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("social_connections")}
    additions = [
        ("destinations_json", "TEXT"),
        ("selected_page_id", "VARCHAR(128)"),
        ("selected_instagram_id", "VARCHAR(128)"),
        ("workspace_id", "VARCHAR(64) DEFAULT 'default'"),
        ("created_by", "VARCHAR(128)"),
    ]
    try:
        with engine.connect() as conn:
            for name, ddl in additions:
                if name in cols:
                    continue
                conn.execute(text(f"ALTER TABLE social_connections ADD COLUMN {name} {ddl}"))
                logger.info("Added social_connections.%s column", name)
            conn.commit()
    except Exception as e:
        logger.warning("Could not ensure social_connections Meta columns", error=str(e))


def _ensure_ai_agent_tasks_negotiation_stage_column() -> None:
    """Add negotiation_stage to ai_agent_tasks for Phase 7 intelligence (existing DBs)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "ai_agent_tasks" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("ai_agent_tasks")}
    if "negotiation_stage" in cols:
        return
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE ai_agent_tasks ADD COLUMN negotiation_stage "
                    "VARCHAR(32) DEFAULT 'opening'"
                )
            )
            conn.execute(
                text(
                    "UPDATE ai_agent_tasks SET negotiation_stage = 'opening' "
                    "WHERE negotiation_stage IS NULL OR negotiation_stage = ''"
                )
            )
            conn.commit()
        logger.info("Added ai_agent_tasks.negotiation_stage column")
    except Exception as e:
        logger.warning(
            "Could not add ai_agent_tasks.negotiation_stage (may already exist)",
            error=str(e),
        )


def _ensure_ai_agent_tasks_auto_loop_columns() -> None:
    """Phase 11: auto_mode, auto_delay_sec, auto_last_run_at on ai_agent_tasks (additive)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "ai_agent_tasks" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("ai_agent_tasks")}
    to_add: list[tuple[str, str]] = []
    if "auto_mode" not in cols:
        to_add.append(("auto_mode", "VARCHAR(16) DEFAULT 'autonomous'"))
    if "auto_delay_sec" not in cols:
        to_add.append(("auto_delay_sec", "INTEGER DEFAULT 20"))
    if "auto_last_run_at" not in cols:
        to_add.append(("auto_last_run_at", "DATETIME"))
    for name, ddl in to_add:
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE ai_agent_tasks ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added ai_agent_tasks column", column=name)
        except Exception as e:
            logger.warning(
                "Could not add ai_agent_tasks.%s (may already exist)",
                name,
                error=str(e),
            )
    insp2 = inspect(engine)
    cols2 = {c["name"] for c in insp2.get_columns("ai_agent_tasks")}
    if "auto_mode" in cols2:
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "UPDATE ai_agent_tasks SET auto_mode = 'autonomous' "
                        "WHERE auto_mode IS NULL OR TRIM(auto_mode) = ''"
                    )
                )
                conn.commit()
        except Exception as e:
            logger.warning("ai_agent_tasks auto_mode backfill skipped", error=str(e))


def _ensure_discovered_users_source_username_column() -> None:
    """Add source_chat_username column to discovered_users if missing (for existing DBs)."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "discovered_users" not in insp.get_table_names():
        return
    cols = [c["name"] for c in insp.get_columns("discovered_users")]
    if "source_chat_username" in cols:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE discovered_users ADD COLUMN source_chat_username VARCHAR(255)"))
            conn.commit()
        logger.info("Added discovered_users.source_chat_username column")
    except Exception as e:
        logger.warning("Could not add source_chat_username column (may already exist)", error=str(e))


def _ensure_scheduled_jobs_lease_columns() -> None:
    """Add lease_until / lease_owner for scheduler DB-level claims (additive)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "scheduled_jobs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("scheduled_jobs")}
    for name, ddl in (
        ("lease_until", "DATETIME"),
        ("lease_owner", "VARCHAR(128)"),
    ):
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE scheduled_jobs ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added scheduled_jobs column", column=name)
        except Exception as e:
            logger.warning(
                "Could not add scheduled_jobs.%s (may already exist)",
                name,
                error=str(e),
            )


def _ensure_scheduled_jobs_dm_columns() -> None:
    """Wave 10: DM peer/body columns + nullable target_id for private peers.

    SQLite cannot DROP NOT NULL; when ``target_id`` is still NOT NULL we rebuild
    the table once (copy all rows) so PROMO/INFO history is preserved and DM
    jobs may omit ``target_id``.
    """
    from sqlalchemy import inspect, text

    if not _is_sqlite(settings.database.url):
        # Non-SQLite: additive columns + ALTER nullable if dialect supports it.
        insp = inspect(engine)
        if "scheduled_jobs" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("scheduled_jobs")}
        for name, ddl in (
            ("peer_id", "VARCHAR(64)"),
            ("peer_type", "VARCHAR(32)"),
            ("message_body", "TEXT"),
            ("schedule_timezone", "VARCHAR(64)"),
        ):
            if name in cols:
                continue
            try:
                with engine.connect() as conn:
                    conn.execute(text(f"ALTER TABLE scheduled_jobs ADD COLUMN {name} {ddl}"))
                    conn.commit()
                logger.info("Added scheduled_jobs column", column=name)
            except Exception as e:
                logger.warning(
                    "Could not add scheduled_jobs.%s (may already exist)",
                    name,
                    error=str(e),
                )
        return

    insp = inspect(engine)
    if "scheduled_jobs" not in insp.get_table_names():
        return

    col_infos = {c["name"]: c for c in insp.get_columns("scheduled_jobs")}
    for name, ddl in (
        ("peer_id", "VARCHAR(64)"),
        ("peer_type", "VARCHAR(32)"),
        ("message_body", "TEXT"),
        ("schedule_timezone", "VARCHAR(64)"),
    ):
        if name in col_infos:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE scheduled_jobs ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added scheduled_jobs column", column=name)
            col_infos[name] = {"name": name, "nullable": True}
        except Exception as e:
            logger.warning(
                "Could not add scheduled_jobs.%s (may already exist)",
                name,
                error=str(e),
            )

    # Refresh after additive ALTERs
    insp = inspect(engine)
    col_infos = {c["name"]: c for c in insp.get_columns("scheduled_jobs")}
    target_col = col_infos.get("target_id")
    if target_col is None:
        return
    # SQLAlchemy inspect: nullable True means NULL allowed
    if target_col.get("nullable", False):
        return

    logger.info(
        "scheduled_jobs_rebuild_for_nullable_target_id",
        reason="Wave 10 DM jobs cannot use chat_targets FK",
    )
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("CREATE TABLE scheduled_jobs__w10 ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "account_id INTEGER NOT NULL, "
                "target_id INTEGER, "
                "type VARCHAR(20) NOT NULL, "
                "run_at DATETIME NOT NULL, "
                "status VARCHAR(20), "
                "template_id INTEGER, "
                "attempts INTEGER, "
                "last_error TEXT, "
                "created_at DATETIME, "
                "updated_at DATETIME, "
                "lease_until DATETIME, "
                "lease_owner VARCHAR(128), "
                "peer_id VARCHAR(64), "
                "peer_type VARCHAR(32), "
                "message_body TEXT, "
                "schedule_timezone VARCHAR(64), "
                "FOREIGN KEY(account_id) REFERENCES accounts (id), "
                "FOREIGN KEY(target_id) REFERENCES chat_targets (id), "
                "FOREIGN KEY(template_id) REFERENCES message_templates (id)"
                ")"))
            conn.execute(text(
                "INSERT INTO scheduled_jobs__w10 ("
                "id, account_id, target_id, type, run_at, status, template_id, attempts, "
                "last_error, created_at, updated_at, lease_until, lease_owner, "
                "peer_id, peer_type, message_body, schedule_timezone) "
                "SELECT id, account_id, target_id, type, run_at, status, template_id, attempts, "
                "last_error, created_at, updated_at, lease_until, lease_owner, "
                "peer_id, peer_type, message_body, schedule_timezone "
                "FROM scheduled_jobs"
            ))
            conn.execute(text("DROP TABLE scheduled_jobs"))
            conn.execute(text("ALTER TABLE scheduled_jobs__w10 RENAME TO scheduled_jobs"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_id ON scheduled_jobs (id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_status_run_at "
                "ON scheduled_jobs (status, run_at)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_lease_owner_lease_until "
                "ON scheduled_jobs (lease_owner, lease_until)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_account_id "
                "ON scheduled_jobs (account_id)"
            ))
            conn.execute(text("PRAGMA foreign_keys=ON"))
        logger.info("scheduled_jobs_rebuild_complete", target_id_nullable=True)
    except Exception as e:
        logger.error("scheduled_jobs_rebuild_failed", error=str(e))
        raise


def _ensure_message_deliveries_send_intent_columns() -> None:
    """Add attempt_started_at / idempotency_key for durable send-intent (additive)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "message_deliveries" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("message_deliveries")}
    for name, ddl in (
        ("attempt_started_at", "DATETIME"),
        ("idempotency_key", "VARCHAR(64)"),
    ):
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE message_deliveries ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added message_deliveries column", column=name)
        except Exception as e:
            logger.warning(
                "Could not add message_deliveries.%s (may already exist)",
                name,
                error=str(e),
            )


def _ensure_story_runs_mention_plan_column() -> None:
    """Persist Dry Run approved mention plan on story_runs (additive JSON)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "story_runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("story_runs")}
    if "mention_plan" in cols:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE story_runs ADD COLUMN mention_plan JSON"))
            conn.commit()
        logger.info("Added story_runs.mention_plan column")
    except Exception as e:
        logger.warning(
            "Could not add story_runs.mention_plan (may already exist)",
            error=str(e),
        )


def _ensure_healthcheck_run_columns() -> None:
    """Add results, progress, error_message to healthcheck_runs if missing (for background jobs)."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "healthcheck_runs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("healthcheck_runs")}
    to_add = [
        ("results", "TEXT"),       # JSON stored as TEXT
        ("progress", "TEXT"),     # JSON
        ("error_message", "TEXT"),
    ]
    for name, ddl in to_add:
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE healthcheck_runs ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added healthcheck_runs column", column=name)
        except Exception as e:
            logger.warning("Could not add healthcheck_runs.%s (may already exist)", name, error=str(e))


def _ensure_accounts_healthcheck_columns() -> None:
    """Add healthcheck columns to accounts if missing (for existing DBs)."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "accounts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("accounts")}
    to_add = [
        ("health_status", "VARCHAR(20)"),
        ("health_reason", "VARCHAR(255)"),
        ("health_message", "TEXT"),
        ("health_checked_at", "DATETIME"),
        ("session_path", "VARCHAR(512)"),
        ("story_blocked_until", "DATETIME"),
        ("story_status", "VARCHAR(20)"),
        ("story_status_reason", "VARCHAR(255)"),
        ("story_status_checked_at", "DATETIME"),
    ]
    added = []
    for name, ddl in to_add:
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}"))
                conn.commit()
            added.append(name)
            logger.info("Added accounts column", column=name)
        except Exception as e:
            logger.warning("Could not add accounts column %s (may already exist)", name, error=str(e))
    if added:
        logger.info("Added accounts columns", columns=added)


def _ensure_accounts_warmup_columns() -> None:
    """Add warmup/precheck columns to accounts if missing (backward-compatible)."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "accounts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("accounts")}
    to_add = [
        ("imported_at", "DATETIME"),
        ("first_seen_at", "DATETIME"),
        ("last_story_attempt_at", "DATETIME"),
        ("successful_story_count", "INTEGER DEFAULT 0"),
        ("failed_story_count", "INTEGER DEFAULT 0"),
        ("warmup_status", "VARCHAR(20)"),
        ("story_precheck_status", "VARCHAR(30)"),
        ("story_precheck_reason", "VARCHAR(255)"),
        ("story_precheck_checked_at", "DATETIME"),
        ("import_source", "VARCHAR(100)"),
        ("risk_notes", "TEXT"),
    ]
    added = []
    for name, ddl in to_add:
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}"))
                conn.commit()
            added.append(name)
            logger.info("Added accounts warmup column", column=name)
        except Exception as e:
            logger.warning("Could not add accounts column %s (may already exist)", name, error=str(e))
    if added:
        logger.info("Added accounts warmup columns", columns=added)


def _ensure_accounts_identity_audit_columns() -> None:
    """Add identity audit columns (last get_me vs DB comparison) if missing."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "accounts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("accounts")}
    to_add = [
        ("identity_audit_status", "VARCHAR(32)"),
        ("identity_audit_reason", "VARCHAR(255)"),
        ("identity_audit_at", "DATETIME"),
    ]
    for name, ddl in to_add:
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added accounts identity audit column", column=name)
        except Exception as e:
            logger.warning("Could not add accounts column %s (may already exist)", name, error=str(e))


def _ensure_accounts_profile_capability_columns() -> None:
    """Add profile/mutation capability columns (separate from health and story precheck)."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "accounts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("accounts")}
    to_add = [
        ("profile_capability_status", "VARCHAR(20)"),
        ("profile_capability_reason", "VARCHAR(255)"),
    ]
    for name, ddl in to_add:
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added accounts profile capability column", column=name)
        except Exception as e:
            logger.warning("Could not add accounts column %s (may already exist)", name, error=str(e))


def _ensure_accounts_safety_columns() -> None:
    """Add profile-tracking and safety columns to accounts if missing."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "accounts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("accounts")}
    to_add = [
        ("username_last_changed_at", "DATETIME"),
        ("profile_photo_last_changed_at", "DATETIME"),
        ("manual_review_required", "INTEGER DEFAULT 0"),
        ("manual_review_reason", "VARCHAR(255)"),
        ("last_story_failure_at", "DATETIME"),
        ("last_story_success_at", "DATETIME"),
        ("story_attempts_today", "INTEGER DEFAULT 0"),
        ("stories_today_on", "DATE"),
    ]
    for name, ddl in to_add:
        if name in cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}"))
                conn.commit()
            logger.info("Added accounts safety column", column=name)
        except Exception as e:
            logger.warning("Could not add accounts column %s (may already exist)", name, error=str(e))


def _ensure_account_governance_tables() -> None:
    """Idempotent governance table ensure (SQLite-safe)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = {
        "account_runtime_roles": """
            CREATE TABLE IF NOT EXISTS account_runtime_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                role VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL,
                created_by VARCHAR(128),
                reason TEXT,
                active BOOLEAN NOT NULL DEFAULT 1,
                FOREIGN KEY (account_id) REFERENCES accounts(id),
                UNIQUE (account_id, role)
            )
        """,
        "account_runtime_tags": """
            CREATE TABLE IF NOT EXISTS account_runtime_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                tag VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL,
                created_by VARCHAR(128),
                active BOOLEAN NOT NULL DEFAULT 1,
                FOREIGN KEY (account_id) REFERENCES accounts(id),
                UNIQUE (account_id, tag)
            )
        """,
        "account_governance_audit_logs": """
            CREATE TABLE IF NOT EXISTS account_governance_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                action VARCHAR(64) NOT NULL,
                target_type VARCHAR(32) NOT NULL,
                target_value VARCHAR(128) NOT NULL,
                reason TEXT,
                actor VARCHAR(128),
                payload_json TEXT,
                created_at DATETIME NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )
        """,
        "account_pinned": """
            CREATE TABLE IF NOT EXISTS account_pinned (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL UNIQUE,
                pinned_at DATETIME NOT NULL,
                pinned_by VARCHAR(128),
                note TEXT,
                active BOOLEAN NOT NULL DEFAULT 1,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )
        """,
        "account_cohorts": """
            CREATE TABLE IF NOT EXISTS account_cohorts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(128) NOT NULL,
                description TEXT,
                created_at DATETIME NOT NULL,
                created_by VARCHAR(128),
                active BOOLEAN NOT NULL DEFAULT 1
            )
        """,
        "account_cohort_members": """
            CREATE TABLE IF NOT EXISTS account_cohort_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cohort_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                added_at DATETIME NOT NULL,
                added_by VARCHAR(128),
                active BOOLEAN NOT NULL DEFAULT 1,
                FOREIGN KEY (cohort_id) REFERENCES account_cohorts(id),
                FOREIGN KEY (account_id) REFERENCES accounts(id),
                UNIQUE (cohort_id, account_id)
            )
        """,
    }
    for name, ddl in tables.items():
        if name in insp.get_table_names():
            continue
        try:
            with engine.connect() as conn:
                conn.execute(text(ddl))
                conn.commit()
            logger.info("Created governance table", table=name)
        except Exception as e:
            logger.warning("Could not create governance table", table=name, error=str(e))


def _ensure_account_risk_events_table() -> None:
    """Create account_risk_events table for audit trail if missing."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "account_risk_events" in insp.get_table_names():
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS account_risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    event_type VARCHAR(50) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    details TEXT,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_risk_events_account_id ON account_risk_events(account_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_risk_events_created_at ON account_risk_events(created_at)"))
            conn.commit()
        logger.info("Created account_risk_events table")
    except Exception as e:
        logger.warning("Could not create account_risk_events table", error=str(e))


def get_db() -> Generator[Session, None, None]:
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """Context manager for database sessions"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_autostory_hardening_schema() -> None:
    """Additive MODEL A columns/tables for unsupervised AutoStory scheduling."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())

    if "auto_story_campaigns" in tables:
        cols = {c["name"] for c in insp.get_columns("auto_story_campaigns")}
        additions = [
            ("claimed_by", "VARCHAR(128)"),
            ("claimed_at", "DATETIME"),
            ("claim_expires_at", "DATETIME"),
            ("authorized_account_ids", "JSON"),
            ("authorization_wave_index", "INTEGER"),
            ("authorization_revoked_at", "DATETIME"),
            ("campaign_mode", "VARCHAR(32) DEFAULT 'accounts_publish_once'"),
            ("stories_per_account_per_day", "INTEGER DEFAULT 1"),
            ("awake_start_hhmm", "VARCHAR(5)"),
            ("awake_end_hhmm", "VARCHAR(5)"),
        ]
        for name, ddl in additions:
            if name in cols:
                continue
            try:
                with engine.connect() as conn:
                    conn.execute(
                        text(f"ALTER TABLE auto_story_campaigns ADD COLUMN {name} {ddl}")
                    )
                    conn.commit()
                logger.info("Added auto_story_campaigns.%s", name)
            except Exception as e:
                logger.warning("Could not add auto_story_campaigns.%s", name, error=str(e))

    if "auto_story_account_progress" in tables:
        cols = {c["name"] for c in insp.get_columns("auto_story_account_progress")}
        if "mention_plan" not in cols:
            try:
                with engine.connect() as conn:
                    conn.execute(
                        text("ALTER TABLE auto_story_account_progress ADD COLUMN mention_plan JSON")
                    )
                    conn.commit()
                logger.info("Added auto_story_account_progress.mention_plan column")
            except Exception as e:
                logger.warning(
                    "Could not add auto_story_account_progress.mention_plan column", error=str(e)
                )

    if "auto_story_account_progress" not in tables:
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS auto_story_account_progress (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          campaign_id INTEGER NOT NULL,
                          wave_index INTEGER NOT NULL DEFAULT 0,
                          account_id INTEGER NOT NULL,
                          run_id INTEGER,
                          story_id INTEGER,
                          telegram_story_id INTEGER,
                          mention_plan JSON,
                          status VARCHAR(32) DEFAULT 'pending',
                          attempt_count INTEGER DEFAULT 0,
                          error TEXT,
                          reconciled_at DATETIME,
                          created_at DATETIME,
                          updated_at DATETIME,
                          UNIQUE(campaign_id, wave_index, account_id)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_autostory_progress_campaign "
                        "ON auto_story_account_progress (campaign_id)"
                    )
                )
                conn.commit()
            logger.info("Created auto_story_account_progress table")
        except Exception as e:
            logger.warning("Could not create auto_story_account_progress", error=str(e))

    if "auto_story_account_locks" not in tables:
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS auto_story_account_locks (
                          account_id INTEGER PRIMARY KEY,
                          campaign_id INTEGER NOT NULL,
                          wave_index INTEGER NOT NULL,
                          locked_at DATETIME,
                          expires_at DATETIME
                        )
                        """
                    )
                )
                conn.commit()
            logger.info("Created auto_story_account_locks table")
        except Exception as e:
            logger.warning("Could not create auto_story_account_locks", error=str(e))

    # Refresh table list after possible creates
    tables = set(inspect(engine).get_table_names())
    if "auto_story_daily_progress" not in tables:
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS auto_story_daily_progress (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          campaign_id INTEGER NOT NULL,
                          account_id INTEGER NOT NULL,
                          local_date DATE NOT NULL,
                          target_count INTEGER NOT NULL DEFAULT 1,
                          successful_count INTEGER NOT NULL DEFAULT 0,
                          failed_count INTEGER NOT NULL DEFAULT 0,
                          ambiguous_count INTEGER NOT NULL DEFAULT 0,
                          last_attempt_at DATETIME,
                          last_story_id INTEGER,
                          completed_for_day BOOLEAN NOT NULL DEFAULT 0,
                          created_at DATETIME,
                          updated_at DATETIME,
                          UNIQUE(campaign_id, account_id, local_date)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_autostory_daily_campaign "
                        "ON auto_story_daily_progress (campaign_id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_autostory_daily_date "
                        "ON auto_story_daily_progress (local_date)"
                    )
                )
                conn.commit()
            logger.info("Created auto_story_daily_progress table")
        except Exception as e:
            logger.warning("Could not create auto_story_daily_progress", error=str(e))
