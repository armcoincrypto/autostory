"""
Tests for Database Models
"""
import pytest
from datetime import datetime

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.models import Account, Story, DiscoveredUser, Campaign, Task
from src.core.models import AccountStatus, TaskStatus, TaskType


class TestAccountModel:
    """Tests for Account model"""

    def test_create_account(self, test_db, sample_account_data):
        """Test creating an account"""
        account = Account(**sample_account_data)
        test_db.add(account)
        test_db.commit()

        assert account.id is not None
        assert account.phone_number == sample_account_data["phone_number"]
        assert account.status == AccountStatus.AUTH_REQUIRED

    def test_account_status_transitions(self, test_db):
        """Test account status changes"""
        account = Account(phone_number="+9876543210")
        test_db.add(account)
        test_db.commit()

        # Initial status
        assert account.status == AccountStatus.AUTH_REQUIRED

        # Change to active
        account.status = AccountStatus.ACTIVE
        test_db.commit()
        assert account.status == AccountStatus.ACTIVE

    def test_account_repr(self, test_db, sample_account_data):
        """Test account string representation"""
        account = Account(**sample_account_data)
        repr_str = repr(account)
        assert sample_account_data["phone_number"] in repr_str


class TestDiscoveredUserModel:
    """Tests for DiscoveredUser model"""

    def test_create_discovered_user(self, test_db):
        """Test creating a discovered user"""
        user = DiscoveredUser(
            user_id=111222333,
            username="discovered_user",
            first_name="John",
            source_chat_id=-100123456,
            source_chat_title="Test Channel"
        )
        test_db.add(user)
        test_db.commit()

        assert user.id is not None
        assert user.times_mentioned == 0
        assert user.is_blocked == False

    def test_increment_mentions(self, test_db):
        """Test incrementing mention count"""
        user = DiscoveredUser(
            user_id=444555666,
            username="mention_test"
        )
        test_db.add(user)
        test_db.commit()

        user.times_mentioned += 1
        user.last_mentioned_at = datetime.utcnow()
        test_db.commit()

        assert user.times_mentioned == 1


class TestCampaignModel:
    """Tests for Campaign model"""

    def test_create_campaign(self, test_db, sample_campaign_data):
        """Test creating a campaign"""
        campaign = Campaign(
            name=sample_campaign_data["name"],
            description=sample_campaign_data["description"]
        )
        test_db.add(campaign)
        test_db.commit()

        assert campaign.id is not None
        assert campaign.is_active == True
        assert campaign.total_stories_published == 0


class TestTaskModel:
    """Tests for Task model"""

    def test_create_task(self, test_db):
        """Test creating a task"""
        task = Task(
            task_type=TaskType.PUBLISH_STORY,
            payload={"media_path": "/test.jpg"}
        )
        test_db.add(task)
        test_db.commit()

        assert task.id is not None
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 0

    def test_task_status_transitions(self, test_db):
        """Test task status changes"""
        task = Task(task_type=TaskType.DISCOVER_USERS)
        test_db.add(task)
        test_db.commit()

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        test_db.commit()

        assert task.status == TaskStatus.RUNNING
        assert task.started_at is not None
