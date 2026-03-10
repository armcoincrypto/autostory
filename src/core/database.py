"""
Database configuration and session management
"""
import os
import sys
import sqlite3
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from sqlalchemy import event
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
        path = rest  # relative: data/storyfleet.db
    if not path or path == ":memory:":
        return ":memory:"
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
    conn = sqlite3.connect(path, timeout=SQLITE_CONNECT_TIMEOUT_SEC)
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

# Create session factory (expire_on_commit=False so Account objects stay usable after session closes)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Initialize database tables"""
    from . import models  # noqa: F401
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    _ensure_accounts_purpose_column()
    _ensure_accounts_healthcheck_columns()
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
