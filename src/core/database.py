"""
Database configuration and session management
"""
import os
import sys
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
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

# Create engine
engine = create_engine(
    settings.database.url,
    pool_size=settings.database.pool_size,
    max_overflow=settings.database.max_overflow,
    pool_pre_ping=True,
    echo=settings.environment == "development",
)

# Create session factory (expire_on_commit=False so Account objects stay usable after session closes)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Initialize database tables"""
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _ensure_accounts_purpose_column()
    _ensure_discovered_users_source_username_column()
    logger.info("Database initialized", tables=list(Base.metadata.tables.keys()))


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
