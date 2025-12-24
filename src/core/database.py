"""
Database configuration and session management
"""
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, declarative_base
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
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

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Initialize database tables"""
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized", tables=list(Base.metadata.tables.keys()))


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
