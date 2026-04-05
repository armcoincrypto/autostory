"""
Pytest Configuration and Fixtures
"""
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.database import Base


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
