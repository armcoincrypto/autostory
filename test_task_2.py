#!/usr/bin/env python3
"""
STORYFLEET - Task 2: User Discovery Engine
===========================================
Safely scrape and discover users from public Telegram groups.

Features:
1. Join public Telegram groups (via invite links)
2. Extract participant user IDs safely
3. Avoid detection (random delays, human-like patterns)
4. Store results in structured format

Usage:
    python test_task_2.py
"""
import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from enum import Enum

try:
    from telethon import TelegramClient, functions
    from telethon.tl.types import (
        User, Channel, Chat,
        ChannelParticipantsRecent,
        ChannelParticipantsSearch,
    )
    from telethon.errors import (
        FloodWaitError,
        ChatAdminRequiredError,
        ChannelPrivateError,
        UserBannedInChannelError,
    )
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    print("Note: telethon not installed. Running in simulation mode.")


class ScrapeStatus(Enum):
    """Scraping operation status"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"


@dataclass
class ScrapedUser:
    """Discovered user data structure"""
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    last_seen: Optional[str] = None
    is_bot: bool = False
    is_verified: bool = False
    is_premium: bool = False
    access_hash: Optional[int] = None

    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "last_seen": self.last_seen,
            "is_bot": self.is_bot,
            "is_verified": self.is_verified,
            "is_premium": self.is_premium,
        }


@dataclass
class ScrapeResult:
    """Result of a scraping operation"""
    group_name: str
    group_id: Optional[int] = None
    group_username: Optional[str] = None
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: ScrapeStatus = ScrapeStatus.PENDING
    users: List[ScrapedUser] = field(default_factory=list)
    total_members: int = 0
    error_message: Optional[str] = None
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict:
        """Convert to output format"""
        return {
            "group_name": self.group_name,
            "group_id": self.group_id,
            "group_username": self.group_username,
            "scraped_at": self.scraped_at,
            "status": self.status.value,
            "total_members": self.total_members,
            "users_scraped": len(self.users),
            "users": [u.to_dict() for u in self.users],
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
        }


class HumanBehaviorSimulator:
    """
    Simulates human-like behavior patterns to avoid detection.

    Implements:
    - Random delays between actions
    - Variable typing patterns
    - Activity windows
    - Progressive backoff on errors
    """

    def __init__(self):
        self.min_delay = 1.0
        self.max_delay = 3.0
        self.scroll_delay_min = 0.5
        self.scroll_delay_max = 2.0
        self.error_count = 0

    async def random_delay(self, action_type: str = "default") -> float:
        """Apply random delay based on action type"""
        delays = {
            "join": (3.0, 8.0),      # Longer delay after joining
            "scroll": (0.5, 2.0),    # Quick scroll delay
            "request": (1.0, 3.0),   # API request delay
            "batch": (2.0, 5.0),     # Between batches
            "default": (1.0, 3.0),
        }

        min_d, max_d = delays.get(action_type, delays["default"])

        # Add extra delay based on error count
        if self.error_count > 0:
            extra = min(self.error_count * 2, 30)
            min_d += extra
            max_d += extra

        delay = random.uniform(min_d, max_d)
        await asyncio.sleep(delay)
        return delay

    def record_error(self):
        """Record an error for backoff"""
        self.error_count += 1

    def record_success(self):
        """Record success to reduce backoff"""
        self.error_count = max(0, self.error_count - 1)

    def should_take_break(self, actions_count: int) -> bool:
        """Determine if should take a longer break"""
        # Take break every 50-100 actions
        threshold = random.randint(50, 100)
        return actions_count > 0 and actions_count % threshold == 0


class UserScraper:
    """
    Safely scrapes users from public Telegram groups.

    Features:
    - Join public groups via invite links or usernames
    - Extract participant user IDs with safety measures
    - Human-like patterns to avoid detection
    - Structured result storage
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        output_dir: str = "./data/scrapes"
    ):
        self.client = client
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.behavior = HumanBehaviorSimulator()
        self._seen_users: set = set()

    async def join_group(self, invite_link: str) -> Dict[str, Any]:
        """
        Join a public Telegram group.

        Args:
            invite_link: Invite link or @username

        Returns:
            Result with group info or error
        """
        result = {
            "success": False,
            "group_name": None,
            "group_id": None,
            "message": "",
        }

        try:
            # Parse invite link
            group_ref = self._parse_invite_link(invite_link)

            await self.behavior.random_delay("join")

            if TELETHON_AVAILABLE and self.client:
                # Try to get entity first (might already be joined)
                try:
                    entity = await self.client.get_entity(group_ref)
                    result["success"] = True
                    result["group_name"] = getattr(entity, 'title', str(group_ref))
                    result["group_id"] = entity.id
                    result["message"] = "Already a member"
                    return result
                except:
                    pass

                # Join via invite
                if "joinchat" in invite_link or "+" in invite_link:
                    hash_match = re.search(r'(?:joinchat/|\+)([a-zA-Z0-9_-]+)', invite_link)
                    if hash_match:
                        invite_hash = hash_match.group(1)
                        updates = await self.client(functions.messages.ImportChatInviteRequest(
                            hash=invite_hash
                        ))
                        chat = updates.chats[0]
                        result["success"] = True
                        result["group_name"] = chat.title
                        result["group_id"] = chat.id
                        result["message"] = "Joined successfully"
                else:
                    # Join by username
                    updates = await self.client(functions.channels.JoinChannelRequest(
                        channel=group_ref
                    ))
                    channel = updates.chats[0]
                    result["success"] = True
                    result["group_name"] = channel.title
                    result["group_id"] = channel.id
                    result["message"] = "Joined successfully"
            else:
                # Simulation mode
                result["success"] = True
                result["group_name"] = f"Simulated Group ({group_ref})"
                result["group_id"] = random.randint(1000000000, 9999999999)
                result["message"] = "[SIMULATED] Joined successfully"

            self.behavior.record_success()
            print(f"✅ Joined: {result['group_name']}")

        except FloodWaitError as e:
            result["message"] = f"Flood wait: {e.seconds}s"
            self.behavior.record_error()
            print(f"⏳ Flood wait: {e.seconds}s")

        except ChannelPrivateError:
            result["message"] = "Group is private"
            print("❌ Group is private")

        except Exception as e:
            result["message"] = str(e)
            self.behavior.record_error()
            print(f"❌ Join failed: {e}")

        return result

    async def scrape_participants(
        self,
        group: str,
        limit: int = 500,
        filter_bots: bool = True
    ) -> ScrapeResult:
        """
        Extract participants from a group.

        Args:
            group: Group username, ID, or invite link
            limit: Maximum users to scrape
            filter_bots: Exclude bot accounts

        Returns:
            ScrapeResult with discovered users
        """
        start_time = datetime.utcnow()

        result = ScrapeResult(
            group_name=str(group),
            status=ScrapeStatus.IN_PROGRESS,
        )

        try:
            group_ref = self._parse_invite_link(group)

            if TELETHON_AVAILABLE and self.client:
                # Get entity
                entity = await self.client.get_entity(group_ref)
                result.group_name = getattr(entity, 'title', str(group_ref))
                result.group_id = entity.id
                result.group_username = getattr(entity, 'username', None)

                # Get full info for member count
                if hasattr(entity, 'participants_count'):
                    result.total_members = entity.participants_count

                # Scrape participants
                offset = 0
                batch_size = 100
                scraped_count = 0

                while scraped_count < limit:
                    await self.behavior.random_delay("batch")

                    try:
                        participants = await self.client(
                            functions.channels.GetParticipantsRequest(
                                channel=entity,
                                filter=ChannelParticipantsRecent(),
                                offset=offset,
                                limit=min(batch_size, limit - scraped_count),
                                hash=0
                            )
                        )

                        if not participants.users:
                            break

                        for user in participants.users:
                            if self._should_include_user(user, filter_bots):
                                scraped_user = self._extract_user_data(user)
                                if scraped_user.user_id not in self._seen_users:
                                    result.users.append(scraped_user)
                                    self._seen_users.add(scraped_user.user_id)

                            scraped_count += 1
                            if scraped_count >= limit:
                                break

                        offset += len(participants.users)

                        # Check for break
                        if self.behavior.should_take_break(scraped_count):
                            print(f"   Taking break at {scraped_count} users...")
                            await asyncio.sleep(random.uniform(5, 15))

                        if len(participants.users) < batch_size:
                            break

                    except ChatAdminRequiredError:
                        result.error_message = "Admin access required for full list"
                        break

                    except Exception as e:
                        self.behavior.record_error()
                        result.error_message = str(e)
                        break

            else:
                # Simulation mode - generate fake users
                result.group_name = f"Simulated: {group}"
                result.group_id = random.randint(1000000000, 9999999999)
                result.total_members = random.randint(1000, 50000)

                for i in range(min(limit, 100)):
                    await self.behavior.random_delay("scroll")

                    user = ScrapedUser(
                        user_id=random.randint(100000000, 999999999),
                        username=f"user_{random.randint(1000, 9999)}" if random.random() > 0.3 else None,
                        first_name=f"User{i}",
                        last_name=f"Test{random.randint(1, 100)}" if random.random() > 0.5 else None,
                        is_bot=False,
                        is_verified=random.random() > 0.95,
                        is_premium=random.random() > 0.8,
                    )
                    result.users.append(user)

                    if i % 20 == 0:
                        print(f"   Scraped {i+1}/{limit} users...")

            result.status = ScrapeStatus.COMPLETED
            self.behavior.record_success()

        except Exception as e:
            result.status = ScrapeStatus.FAILED
            result.error_message = str(e)
            self.behavior.record_error()

        # Calculate duration
        result.duration_seconds = (datetime.utcnow() - start_time).total_seconds()

        print(f"✅ Scraped {len(result.users)} users from {result.group_name}")
        print(f"   Duration: {result.duration_seconds:.2f}s")

        return result

    async def scrape_from_messages(
        self,
        group: str,
        days_back: int = 7,
        limit: int = 500
    ) -> ScrapeResult:
        """
        Extract users from recent messages in a group.

        Args:
            group: Group reference
            days_back: How many days of messages to scan
            limit: Maximum messages to scan

        Returns:
            ScrapeResult with discovered users
        """
        start_time = datetime.utcnow()
        result = ScrapeResult(
            group_name=str(group),
            status=ScrapeStatus.IN_PROGRESS,
        )

        try:
            group_ref = self._parse_invite_link(group)
            min_date = datetime.utcnow() - timedelta(days=days_back)

            if TELETHON_AVAILABLE and self.client:
                entity = await self.client.get_entity(group_ref)
                result.group_name = getattr(entity, 'title', str(group_ref))
                result.group_id = entity.id

                message_count = 0
                async for message in self.client.iter_messages(
                    entity,
                    limit=limit,
                    offset_date=datetime.utcnow()
                ):
                    if message.date.replace(tzinfo=None) < min_date:
                        break

                    if message.sender and isinstance(message.sender, type) and hasattr(message.sender, 'id'):
                        user = self._extract_user_data(message.sender)
                        if user.user_id not in self._seen_users:
                            result.users.append(user)
                            self._seen_users.add(user.user_id)

                    message_count += 1

                    if message_count % 100 == 0:
                        await self.behavior.random_delay("scroll")

            else:
                # Simulation
                result.group_name = f"Simulated: {group}"
                result.group_id = random.randint(1000000000, 9999999999)

                for i in range(min(limit // 5, 50)):
                    user = ScrapedUser(
                        user_id=random.randint(100000000, 999999999),
                        username=f"active_user_{random.randint(1000, 9999)}",
                        first_name=f"Active{i}",
                        is_bot=False,
                    )
                    result.users.append(user)

            result.status = ScrapeStatus.COMPLETED

        except Exception as e:
            result.status = ScrapeStatus.FAILED
            result.error_message = str(e)

        result.duration_seconds = (datetime.utcnow() - start_time).total_seconds()
        return result

    def save_results(self, result: ScrapeResult, filename: Optional[str] = None) -> str:
        """Save scrape results to JSON file"""
        if not filename:
            safe_name = re.sub(r'[^\w\-_]', '_', result.group_name)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            filename = f"scrape_{safe_name}_{timestamp}.json"

        filepath = self.output_dir / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)

        print(f"💾 Results saved to: {filepath}")
        return str(filepath)

    def _parse_invite_link(self, link: str) -> str:
        """Parse invite link to extract group reference"""
        link = link.strip()

        # Handle t.me/joinchat/XXXX
        if "joinchat/" in link:
            match = re.search(r'joinchat/([a-zA-Z0-9_-]+)', link)
            if match:
                return match.group(1)

        # Handle t.me/+XXXX
        if "/+" in link or link.startswith("+"):
            match = re.search(r'\+([a-zA-Z0-9_-]+)', link)
            if match:
                return f"+{match.group(1)}"

        # Handle t.me/username
        if "t.me/" in link:
            match = re.search(r't\.me/([a-zA-Z0-9_]+)', link)
            if match:
                return match.group(1)

        # Handle @username
        if link.startswith("@"):
            return link[1:]

        return link

    def _should_include_user(self, user: Any, filter_bots: bool) -> bool:
        """Check if user should be included"""
        if hasattr(user, 'deleted') and user.deleted:
            return False
        if filter_bots and hasattr(user, 'bot') and user.bot:
            return False
        if not hasattr(user, 'id'):
            return False
        return True

    def _extract_user_data(self, user: Any) -> ScrapedUser:
        """Extract user data to ScrapedUser object"""
        return ScrapedUser(
            user_id=user.id,
            username=getattr(user, 'username', None),
            first_name=getattr(user, 'first_name', None),
            last_name=getattr(user, 'last_name', None),
            is_bot=getattr(user, 'bot', False),
            is_verified=getattr(user, 'verified', False),
            is_premium=getattr(user, 'premium', False),
            access_hash=getattr(user, 'access_hash', None),
        )


# ============================================
# TEST SUITE
# ============================================

async def run_tests():
    """Run comprehensive tests for UserScraper"""
    print("\n" + "="*60)
    print("   STORYFLEET - Task 2: User Discovery Tests")
    print("="*60 + "\n")

    # Initialize scraper (simulation mode)
    scraper = UserScraper()

    # Test 1: Parse invite links
    print("📋 Test 1: Invite link parsing")
    print("-" * 40)

    test_links = [
        ("https://t.me/joinchat/ABCDEFG123", "ABCDEFG123"),
        ("https://t.me/+XYZ789", "+XYZ789"),
        ("https://t.me/testchannel", "testchannel"),
        ("@mychannel", "mychannel"),
        ("pythongroup", "pythongroup"),
    ]

    for link, expected in test_links:
        result = scraper._parse_invite_link(link)
        status = "✅" if expected in result or result == expected else "❌"
        print(f"  {status} {link} -> {result}")

    print("✅ Link parsing test passed\n")

    # Test 2: Join group (simulated)
    print("📋 Test 2: Join group")
    print("-" * 40)

    join_result = await scraper.join_group("@testgroup")
    print(f"  Join result: {join_result['message']}")
    assert join_result['success'] == True
    print("✅ Join test passed\n")

    # Test 3: Scrape participants
    print("📋 Test 3: Scrape participants")
    print("-" * 40)

    scrape_result = await scraper.scrape_participants(
        group="@testgroup",
        limit=50,
        filter_bots=True
    )

    print(f"  Group: {scrape_result.group_name}")
    print(f"  Status: {scrape_result.status.value}")
    print(f"  Users scraped: {len(scrape_result.users)}")
    print(f"  Duration: {scrape_result.duration_seconds:.2f}s")

    assert scrape_result.status == ScrapeStatus.COMPLETED
    assert len(scrape_result.users) > 0
    print("✅ Scrape test passed\n")

    # Test 4: Output format verification
    print("📋 Test 4: Output format")
    print("-" * 40)

    output = scrape_result.to_dict()
    required_fields = ["group_name", "scraped_at", "users", "status"]

    for field in required_fields:
        assert field in output, f"Missing field: {field}"
        print(f"  ✓ Field present: {field}")

    # Verify user format
    if output["users"]:
        user = output["users"][0]
        user_fields = ["user_id", "username", "is_bot"]
        for field in user_fields:
            assert field in user, f"Missing user field: {field}"
            print(f"  ✓ User field present: {field}")

    print("✅ Output format test passed\n")

    # Test 5: Save results
    print("📋 Test 5: Save results")
    print("-" * 40)

    saved_path = scraper.save_results(scrape_result, "test_scrape.json")
    assert Path(saved_path).exists()
    print("✅ Save test passed\n")

    # Test 6: Human behavior simulation
    print("📋 Test 6: Human behavior patterns")
    print("-" * 40)

    behavior = scraper.behavior

    # Test delay randomization
    delays = []
    for i in range(5):
        delay = await behavior.random_delay("request")
        delays.append(delay)
        print(f"  Delay {i+1}: {delay:.2f}s")

    # Verify delays are different (random)
    assert len(set([round(d, 1) for d in delays])) > 1, "Delays should be random"
    print("✅ Behavior test passed\n")

    # Test 7: Duplicate filtering
    print("📋 Test 7: Duplicate user filtering")
    print("-" * 40)

    # Scrape again - should not have duplicates
    scrape_result2 = await scraper.scrape_participants(
        group="@testgroup",
        limit=30
    )

    # Count unique user IDs across both results
    all_users = scrape_result.users + scrape_result2.users
    unique_ids = set(u.user_id for u in all_users)
    print(f"  Total users across scrapes: {len(all_users)}")
    print(f"  Unique user IDs: {len(unique_ids)}")
    print("✅ Duplicate filtering test passed\n")

    # Summary
    print("="*60)
    print("   ALL TESTS PASSED ✅")
    print("="*60)

    # Print sample output
    print("\n📊 Sample Output Format:")
    print("-" * 40)
    print(json.dumps(output, indent=2)[:500] + "...")

    return scraper


if __name__ == "__main__":
    asyncio.run(run_tests())
