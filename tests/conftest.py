"""
Pytest Configuration and Fixtures

Isolation is applied before any application import so tests cannot load
production /opt/autostory/.env, SQLite, or session keys.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

# --- Fail-closed isolation (must run before src imports / load_dotenv) ---
_PROD_DB = Path("/opt/autostory/data/storyfleet.db")
_PROD_ENV = Path("/opt/autostory/.env")

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["CAMPAIGN_EXECUTION_ENABLED"] = "false"
os.environ["CONTROLLED_STORY_EXECUTION_ENABLED"] = "false"
os.environ["SCHEDULER_STORY_EXECUTION_ENABLED"] = "false"
os.environ["STORY_MUTATIONS_ENABLED"] = "false"
os.environ["STORY_EXECUTION_ENABLED"] = "false"
os.environ["STORY_EXECUTION_MODE"] = "disabled"
os.environ["AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED"] = "false"
os.environ["META_FACEBOOK_PUBLISHING_ENABLED"] = "false"
os.environ["META_INSTAGRAM_PUBLISHING_ENABLED"] = "false"
os.environ["META_PUBLISHING_EXECUTION_MODE"] = "disabled"
os.environ["SCHEDULER_MUTATIONS_ENABLED"] = "false"
os.environ["DISCOVERY_EXECUTION_ENABLED"] = "false"
os.environ["STORAGE_SESSIONS_DIR"] = str(Path("/tmp/autostory-pytest-sessions"))
os.environ["STORAGE_MEDIA_DIR"] = str(Path("/tmp/autostory-pytest-media"))
Path(os.environ["STORAGE_SESSIONS_DIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["STORAGE_MEDIA_DIR"]).mkdir(parents=True, exist_ok=True)

_TEST_ONLY_KEY_B64 = base64.urlsafe_b64encode(b"S" * 32).decode("ascii")
os.environ["TELEGRAM_SESSION_ENCRYPTION_MODE"] = "encrypted-only"
os.environ["TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID"] = "pytest-test-only"
os.environ["TELEGRAM_SESSION_ENCRYPTION_KEY_B64"] = _TEST_ONLY_KEY_B64

_db_url = os.environ.get("DATABASE_URL") or ""
if "opt/autostory/data" in _db_url.replace("\\", "/") or (
    _PROD_DB.exists() and _db_url.startswith("sqlite") and "storyfleet.db" in _db_url
    and "memory" not in _db_url
):
    raise SystemExit("TEST_DB_IS_PRODUCTION=YES — refusing to run pytest against production SQLite")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.database import Base


def pytest_configure(config):  # noqa: ARG001
    """Re-assert test-only encryption and in-memory DB (never a production secret)."""
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    if not os.environ.get("TELEGRAM_SESSION_ENCRYPTION_KEY_B64") and not os.environ.get(
        "TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON"
    ):
        os.environ["TELEGRAM_SESSION_ENCRYPTION_MODE"] = "encrypted-only"
        os.environ["TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID"] = "pytest-test-only"
        os.environ["TELEGRAM_SESSION_ENCRYPTION_KEY_B64"] = _TEST_ONLY_KEY_B64
    db_url = os.environ.get("DATABASE_URL") or ""
    if "opt/autostory/data" in db_url.replace("\\", "/"):
        raise SystemExit("TEST_DB_IS_PRODUCTION=YES")


@pytest.fixture(scope="session")
def test_db():
    """Create a test database"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    TestSession = sessionmaker(bind=engine)
    session = TestSession()

    yield session

    session.close()


@pytest.fixture
def sample_account_data():
    """Sample account data for testing"""
    return {
        "phone_number": "+1234567890",
        "user_id": 123456789,
        "username": "test_user",
        "first_name": "Test",
        "last_name": "User",
    }


@pytest.fixture
def sample_story_data():
    """Sample story data for testing"""
    return {
        "media_path": "/tmp/test_image.jpg",
        "caption": "Test story caption",
        "mentions": [111111, 222222],
    }


@pytest.fixture
def sample_campaign_data():
    """Sample campaign data for testing"""
    return {
        "name": "Test Campaign",
        "description": "Test campaign description",
        "target_chat_ids": ["@testchannel"],
        "story_templates": [{"id": "test", "caption": "Test {variable}"}],
    }
