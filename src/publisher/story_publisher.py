"""
Story Publisher - Publish stories with user mentions
Core feature for STORYFLEET
"""
import asyncio
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.stories import (
    SendStoryRequest,
    GetAllStoriesRequest,
)
from telethon.tl.types import (
    InputMediaUploadedPhoto,
    InputMediaUploadedDocument,
    InputPrivacyValueAllowAll,
    InputPrivacyValueAllowContacts,
    InputPrivacyValueDisallowAll,
    DocumentAttributeVideo,
    User,
    InputPeerSelf,
    MessageEntityMention,
)
from telethon.errors import (
    FloodWaitError,
    UserBannedInChannelError,
    MediaEmptyError,
    FilePartsInvalidError,
)
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Account, Story, DiscoveredUser, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)


class StoryPublisher:
    """
    Publishes stories to Telegram with user mentions

    Features:
    - Upload photo/video as story
    - Add mentions to discovered users
    - Track published stories
    - Handle rate limits with retry
    - Rotate between multiple accounts
    - Rate limiting to prevent bans
    """

    def __init__(self):
        self._clients: Dict[int, TelegramClient] = {}
        self._rate_limits: Dict[int, List[datetime]] = {}  # account_id -> [timestamps]
        self._max_stories_per_hour = 3  # Conservative limit

    def _check_rate_limit(self, account_id: int) -> bool:
        """Check if account is within rate limits"""
        now = datetime.utcnow()
        hour_ago = now - timedelta(hours=1)

        # Get timestamps for this account
        if account_id not in self._rate_limits:
            self._rate_limits[account_id] = []

        # Filter out old timestamps
        self._rate_limits[account_id] = [
            ts for ts in self._rate_limits[account_id]
            if ts > hour_ago
        ]

        # Check if under limit
        return len(self._rate_limits[account_id]) < self._max_stories_per_hour

    def _record_publish(self, account_id: int):
        """Record a successful publish for rate limiting"""
        if account_id not in self._rate_limits:
            self._rate_limits[account_id] = []
        self._rate_limits[account_id].append(datetime.utcnow())

    async def get_client_for_account(self, account_id: int) -> Optional[TelegramClient]:
        """Get or create a Telegram client for an account"""
        if account_id in self._clients:
            client = self._clients[account_id]
            if client.is_connected():
                return client

        # Get account from database
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.session_string:
                return None
            session_string = account.session_string

        # Create client
        client = TelegramClient(
            StringSession(session_string),
            settings.telegram.api_id,
            settings.telegram.api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            logger.warning("Account not authorized", account_id=account_id)
            return None

        self._clients[account_id] = client
        return client

    async def publish_story(
        self,
        account_id: int,
        media_path: str,
        caption: str = "",
        mention_user_ids: List[Any] = None,  # Can be list of ints or list of dicts
        privacy: str = "public",  # public, contacts, private
        retry_count: int = 3,
    ) -> Dict[str, Any]:
        """
        Publish a story with optional mentions

        Args:
            account_id: Account to publish from
            media_path: Path to photo or video file
            caption: Story caption text
            mention_user_ids: List of user_ids (int) or dicts with user_id/username
            privacy: Story privacy setting
            retry_count: Number of retries on failure

        Returns:
            Result dictionary with success status and details
        """
        result = {
            "success": False,
            "account_id": account_id,
            "story_id": None,
            "mentions_added": 0,
            "mentioned_user_ids": [],
            "error": None,
        }

        mention_user_ids = mention_user_ids or []

        # Check rate limit
        if not self._check_rate_limit(account_id):
            result["error"] = f"Rate limit: Account #{account_id} already posted {self._max_stories_per_hour} stories this hour"
            return result

        # Validate media file
        if not os.path.exists(media_path):
            result["error"] = f"Media file not found: {media_path}"
            return result

        # Get client
        client = await self.get_client_for_account(account_id)
        if not client:
            result["error"] = "Failed to get client for account"
            return result

        for attempt in range(retry_count):
            try:
                # Upload media
                logger.info(
                    "Uploading media",
                    account_id=account_id,
                    media_path=media_path,
                    attempt=attempt + 1
                )

                # Determine media type
                ext = Path(media_path).suffix.lower()
                is_video = ext in ['.mp4', '.mov', '.avi', '.mkv', '.webm']

                # Upload file
                file = await client.upload_file(media_path)

                if is_video:
                    # Get video dimensions (default 720x1280 for stories)
                    media = InputMediaUploadedDocument(
                        file=file,
                        mime_type='video/mp4',
                        attributes=[
                            DocumentAttributeVideo(
                                duration=15,  # Default duration
                                w=720,
                                h=1280,
                                supports_streaming=True,
                            )
                        ]
                    )
                else:
                    media = InputMediaUploadedPhoto(file=file)

                # Build caption with @username mentions (plain text, no entities)
                final_caption = caption
                mentioned_ids = []

                if mention_user_ids:
                    # Build @username mention list
                    mention_usernames = []

                    for user_data in mention_user_ids[:10]:  # Max 10 mentions per story
                        try:
                            # Handle both dict format and int format
                            if isinstance(user_data, dict):
                                user_id = user_data.get("user_id")
                                username = user_data.get("username")
                            else:
                                # Legacy: just user_id, skip (can't mention without username)
                                continue

                            if username:
                                mention_usernames.append(f"@{username}")
                                mentioned_ids.append(user_id)
                                result["mentions_added"] += 1
                                logger.info("Added mention", username=username, user_id=user_id)

                        except Exception as e:
                            logger.warning("Failed to process mention", error=str(e))

                    # Add mentions to caption as plain text
                    if mention_usernames:
                        mentions_text = " ".join(mention_usernames)
                        if caption:
                            final_caption = caption + "\n\n" + mentions_text
                        else:
                            final_caption = mentions_text

                result["mentioned_user_ids"] = mentioned_ids

                # Set privacy rules
                privacy_rules = self._get_privacy_rules(privacy)

                # Send story WITHOUT entities (entities require Premium)
                story_result = await client(SendStoryRequest(
                    peer=InputPeerSelf(),
                    media=media,
                    caption=final_caption if final_caption else None,
                    privacy_rules=privacy_rules,
                    pinned=False,
                    noforwards=False,
                ))

                # Extract story ID
                story_id = getattr(story_result, 'id', None)
                if hasattr(story_result, 'updates'):
                    for update in story_result.updates:
                        if hasattr(update, 'story'):
                            story_id = update.story.id
                            break

                result["success"] = True
                result["story_id"] = story_id

                # Record for rate limiting
                self._record_publish(account_id)

                # Save to database
                await self._save_story_record(
                    account_id=account_id,
                    story_id=story_id,
                    caption=final_caption,
                    media_path=media_path,
                    mentions=mentioned_ids,
                )

                logger.info(
                    "Story published successfully",
                    account_id=account_id,
                    story_id=story_id,
                    mentions=result["mentions_added"]
                )

                return result

            except FloodWaitError as e:
                wait_time = e.seconds
                logger.warning(
                    "Flood wait",
                    account_id=account_id,
                    wait_seconds=wait_time,
                    attempt=attempt + 1
                )

                if attempt < retry_count - 1:
                    # Wait and retry
                    await asyncio.sleep(min(wait_time, 300))  # Max 5 min wait
                else:
                    result["error"] = f"Rate limited. Wait {wait_time} seconds."
                    # Mark account as flood wait
                    await self._mark_account_flood_wait(account_id, wait_time)

            except MediaEmptyError:
                result["error"] = "Invalid media file"
                break

            except Exception as e:
                logger.error(
                    "Story publish failed",
                    account_id=account_id,
                    error=str(e),
                    attempt=attempt + 1
                )

                if attempt < retry_count - 1:
                    # Exponential backoff
                    await asyncio.sleep(2 ** attempt)
                else:
                    result["error"] = str(e)

        return result

    def _get_privacy_rules(self, privacy: str) -> List:
        """Get privacy rules for story"""
        if privacy == "contacts":
            return [InputPrivacyValueAllowContacts()]
        elif privacy == "private":
            return [InputPrivacyValueDisallowAll()]
        else:  # public
            return [InputPrivacyValueAllowAll()]

    async def _save_story_record(
        self,
        account_id: int,
        story_id: int,
        caption: str,
        media_path: str,
        mentions: List[int],
    ):
        """Save story record to database"""
        try:
            with get_db_context() as db:
                # Update account story count
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    account.stories_today += 1
                    account.last_active = datetime.utcnow()

                # Determine media type from file extension
                ext = Path(media_path).suffix.lower() if media_path else ""
                is_video = ext in ['.mp4', '.mov', '.avi', '.mkv', '.webm']
                media_type = "video" if is_video else "photo"

                # Get usernames for mentioned users
                mentioned_usernames = []
                for user_id in mentions:
                    user = db.query(DiscoveredUser).filter(
                        DiscoveredUser.user_id == user_id
                    ).first()
                    if user:
                        user.times_mentioned += 1
                        user.last_mentioned_at = datetime.utcnow()
                        if user.username:
                            mentioned_usernames.append(user.username)

                # Create story record with correct fields
                story = Story(
                    account_id=account_id,
                    story_id=story_id,
                    caption=caption,
                    media_path=media_path,
                    media_type=media_type,
                    mentioned_user_ids=mentions,
                    mentioned_usernames=mentioned_usernames,
                    published_at=datetime.utcnow(),
                )
                db.add(story)

                db.commit()
                logger.info("Story record saved", story_id=story_id, mentions_count=len(mentions))

        except Exception as e:
            logger.error("Failed to save story record", error=str(e))

    async def _mark_account_flood_wait(self, account_id: int, wait_seconds: int):
        """Mark account as rate limited"""
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    account.status = AccountStatus.FLOOD_WAIT
                    account.flood_wait_until = datetime.utcnow() + timedelta(seconds=wait_seconds)
                    db.commit()
        except Exception as e:
            logger.error("Failed to mark flood wait", error=str(e))

    async def get_available_account(self) -> Optional[int]:
        """Get an available account for publishing (not rate limited)"""
        with get_db_context() as db:
            now = datetime.utcnow()

            account = db.query(Account).filter(
                Account.status == AccountStatus.ACTIVE,
                Account.session_string.isnot(None),
                Account.stories_today < settings.telegram.max_stories_per_hour,
            ).order_by(
                Account.stories_today.asc(),  # Prefer accounts with fewer stories today
                Account.last_active.asc(),  # Prefer least recently used
            ).first()

            if account:
                return account.id

        return None

    async def get_users_for_mention(self, count: int = 5) -> List[Dict[str, Any]]:
        """Get users suitable for mentioning (with usernames)"""
        with get_db_context() as db:
            users = db.query(DiscoveredUser).filter(
                DiscoveredUser.is_blocked == False,
                DiscoveredUser.times_mentioned == 0,
                DiscoveredUser.username.isnot(None),  # Only users with usernames
            ).order_by(
                DiscoveredUser.discovered_at.desc()
            ).limit(count).all()

            # Return both user_id and username for mention
            return [{"user_id": u.user_id, "username": u.username} for u in users]

    async def publish_with_auto_mentions(
        self,
        media_path: str,
        caption: str = "",
        mentions_count: int = 5,
        account_id: int = None,
    ) -> Dict[str, Any]:
        """
        Convenience method: Publish story with automatic user selection

        Args:
            media_path: Path to media file
            caption: Story caption
            mentions_count: Number of users to mention
            account_id: Specific account to use (or auto-select)

        Returns:
            Publish result
        """
        # Get account
        if account_id is None:
            account_id = await self.get_available_account()
            if not account_id:
                return {
                    "success": False,
                    "error": "No available accounts"
                }

        # Get users to mention
        mention_users = await self.get_users_for_mention(mentions_count)

        # Publish
        return await self.publish_story(
            account_id=account_id,
            media_path=media_path,
            caption=caption,
            mention_user_ids=mention_users,
        )

    async def close(self):
        """Close all client connections"""
        for client in self._clients.values():
            try:
                await client.disconnect()
            except:
                pass
        self._clients.clear()


# Global instance
story_publisher = StoryPublisher()
