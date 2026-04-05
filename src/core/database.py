"""
Database configuration and session management
"""
import os
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
SQLITE_CONNECT_TIMEOUT_SEC = 30


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
    return conn


def _make_engine():
    url = settings.database.url
    kwargs = {
        "echo": settings.environment == "development",
    }
    if _is_sqlite(url):
        # Use creator so every connection gets WAL + busy_timeout at open (active in all entrypoints)
        kwargs["creator"] = _sqlite_creator
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
                "db_sqlite_lock_retry",
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


def init_db() -> None:
    """Initialize database tables"""
    from . import models  # noqa: F401
    import src.dashboard.models  # noqa: F401 - ensures dashboard_users table
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    _ensure_accounts_purpose_column()
    _ensure_accounts_healthcheck_columns()
    _ensure_healthcheck_run_columns()
    _ensure_accounts_warmup_columns()
    _ensure_accounts_safety_columns()
    _ensure_accounts_identity_audit_columns()
    _ensure_accounts_profile_capability_columns()
    _ensure_account_risk_events_table()
    _ensure_discovered_users_source_username_column()
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
