"""
Story Publisher - Publish stories with mentions
"""
import asyncio
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any, Union

from telethon import functions, types
from telethon.tl.types import (
    InputMediaUploadedPhoto,
    InputMediaUploadedDocument,
    InputPrivacyValueAllowAll,
    InputPrivacyValueAllowContacts,
    InputPrivacyValueDisallowAll,
)
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Story, DiscoveredUser, Account, Campaign
from src.core.database import get_db_context
from src.clients.manager import TelegramClientWrapper, client_manager
from src.clients.rate_limiter import AntiDetection

logger = structlog.get_logger(__name__)


class StoryPrivacy:
    """Story privacy settings"""
    PUBLIC = "public"
    CONTACTS = "contacts"
    CLOSE_FRIENDS = "close_friends"
    SELECTED = "selected"


class StoryPublisher:
    """
    Publishes stories with mentions to Telegram

    Features:
    - Photo and video stories
    - Mention tagging
    - Privacy controls
    - Batch publishing across accounts
    - Smart scheduling
    """

    def __init__(self):
        self._media_dir = Path(settings.storage.media_dir)
        self._media_dir.mkdir(parents=True, exist_ok=True)

    async def publish_story(
        self,
        client_wrapper: TelegramClientWrapper,
        media_path: str,
        caption: Optional[str] = None,
        mentions: Optional[List[Union[int, str]]] = None,
        privacy: str = StoryPrivacy.PUBLIC,
        pin_to_profile: bool = False,
        campaign_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Publish a story with optional mentions

        Args:
            client_wrapper: The Telegram client wrapper
            media_path: Path to media file (photo/video)
            caption: Story caption
            mentions: List of user IDs or usernames to mention
            privacy: Privacy setting
            pin_to_profile: Pin story to profile
            campaign_id: Associated campaign ID

        Returns:
            Dict with story details or error
        """
        client = client_wrapper.client
        account = client_wrapper.account

        try:
            # Validate media
            media_file = Path(media_path)
            if not media_file.exists():
                return {"success": False, "error": f"Media file not found: {media_path}"}

            # Determine media type
            media_type = self._get_media_type(media_file)
            if not media_type:
                return {"success": False, "error": "Unsupported media type"}

            # Process mentions
            mentioned_entities = []
            mentioned_user_ids = []
            mentioned_usernames = []

            if mentions:
                for mention in mentions[:settings.telegram.max_mentions_per_story]:
                    try:
                        entity = await client.get_entity(mention)
                        mentioned_entities.append(entity)
                        mentioned_user_ids.append(entity.id)
                        if hasattr(entity, 'username') and entity.username:
                            mentioned_usernames.append(entity.username)
                    except Exception as e:
                        logger.warning("Could not resolve mention", mention=mention, error=str(e))

            # Build caption with mentions
            formatted_caption = self._format_caption_with_mentions(
                caption or "",
                mentioned_entities
            )

            # Upload media
            logger.info(
                "Uploading story media",
                account_id=account.id,
                media_type=media_type,
                mentions=len(mentioned_entities)
            )

            uploaded_media = await client.upload_file(str(media_file))

            # Set privacy
            privacy_rules = self._get_privacy_rules(privacy)

            # Publish story
            await AntiDetection.random_pause(0.5, 1.5)

            if media_type == "photo":
                media = InputMediaUploadedPhoto(file=uploaded_media)
            else:
                media = InputMediaUploadedDocument(
                    file=uploaded_media,
                    mime_type=self._get_mime_type(media_file),
                    attributes=[]
                )

            result = await client(functions.stories.SendStoryRequest(
                peer=types.InputPeerSelf(),
                media=media,
                caption=formatted_caption,
                privacy_rules=privacy_rules,
                pinned=pin_to_profile,
            ))

            # Extract story ID from result
            story_id = None
            if hasattr(result, 'updates'):
                for update in result.updates:
                    if hasattr(update, 'story') and hasattr(update.story, 'id'):
                        story_id = update.story.id
                        break

            # Save to database
            with get_db_context() as db:
                story = Story(
                    account_id=account.id,
                    campaign_id=campaign_id,
                    media_type=media_type,
                    media_path=str(media_path),
                    caption=caption,
                    mentioned_user_ids=mentioned_user_ids,
                    mentioned_usernames=mentioned_usernames,
                    story_id=story_id,
                    published_at=datetime.utcnow(),
                    expires_at=datetime.utcnow() + timedelta(hours=24),
                )
                db.add(story)

                # Update discovered users mention count
                for user_id in mentioned_user_ids:
                    discovered = db.query(DiscoveredUser).filter(
                        DiscoveredUser.user_id == user_id
                    ).first()
                    if discovered:
                        discovered.times_mentioned += 1
                        discovered.last_mentioned_at = datetime.utcnow()

                # Update account stats
                account_db = db.query(Account).filter(Account.id == account.id).first()
                if account_db:
                    account_db.stories_today += 1
                    account_db.last_active = datetime.utcnow()

                db.commit()
                story_db_id = story.id

            logger.info(
                "Story published successfully",
                account_id=account.id,
                story_id=story_id,
                mentions=len(mentioned_user_ids)
            )

            return {
                "success": True,
                "story_id": story_id,
                "db_id": story_db_id,
                "mentions": mentioned_user_ids,
                "message": f"Story published with {len(mentioned_user_ids)} mentions"
            }

        except Exception as e:
            logger.error(
                "Failed to publish story",
                account_id=account.id,
                error=str(e)
            )
            return {"success": False, "error": str(e)}

    async def publish_batch(
        self,
        media_path: str,
        caption: Optional[str] = None,
        mentions_per_story: int = 5,
        max_stories: int = 10,
        campaign_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Publish stories across multiple accounts with distributed mentions

        Args:
            media_path: Path to media file
            caption: Story caption
            mentions_per_story: Number of mentions per story
            max_stories: Maximum number of stories to publish
            campaign_id: Associated campaign

        Returns:
            Batch results
        """
        results = {
            "total_attempted": 0,
            "successful": 0,
            "failed": 0,
            "stories": [],
            "errors": []
        }

        # Get available clients
        clients = await client_manager.get_available_clients()
        if not clients:
            return {"success": False, "error": "No available clients"}

        # Get unmentioned users
        with get_db_context() as db:
            users = db.query(DiscoveredUser).filter(
                DiscoveredUser.is_blocked == False,
                DiscoveredUser.times_mentioned == 0
            ).order_by(DiscoveredUser.discovered_at.desc()).limit(
                max_stories * mentions_per_story
            ).all()

            user_ids = [u.user_id for u in users]

        if not user_ids:
            logger.warning("No users available for mentions")

        # Distribute mentions across stories
        mention_chunks = [
            user_ids[i:i + mentions_per_story]
            for i in range(0, len(user_ids), mentions_per_story)
        ]

        # Publish stories
        story_count = 0
        client_index = 0

        for mentions in mention_chunks[:max_stories]:
            if story_count >= max_stories:
                break

            client_wrapper = clients[client_index % len(clients)]
            client_index += 1

            results["total_attempted"] += 1

            result = await self.publish_story(
                client_wrapper=client_wrapper,
                media_path=media_path,
                caption=caption,
                mentions=mentions,
                campaign_id=campaign_id,
            )

            if result["success"]:
                results["successful"] += 1
                results["stories"].append(result)
            else:
                results["failed"] += 1
                results["errors"].append(result.get("error", "Unknown error"))

            story_count += 1

            # Wait between stories
            await asyncio.sleep(random.uniform(10, 30))

        results["success"] = results["successful"] > 0
        return results

    async def delete_story(
        self,
        client_wrapper: TelegramClientWrapper,
        story_id: int
    ) -> bool:
        """Delete a story"""
        try:
            await client_wrapper.client(functions.stories.DeleteStoriesRequest(
                peer=types.InputPeerSelf(),
                id=[story_id]
            ))

            # Update database
            with get_db_context() as db:
                story = db.query(Story).filter(
                    Story.story_id == story_id,
                    Story.account_id == client_wrapper.account.id
                ).first()
                if story:
                    story.is_deleted = True

            logger.info("Story deleted", story_id=story_id)
            return True

        except Exception as e:
            logger.error("Failed to delete story", story_id=story_id, error=str(e))
            return False

    async def get_story_views(
        self,
        client_wrapper: TelegramClientWrapper,
        story_id: int
    ) -> Optional[int]:
        """Get view count for a story"""
        try:
            result = await client_wrapper.client(functions.stories.GetStoriesViewsRequest(
                peer=types.InputPeerSelf(),
                id=[story_id]
            ))

            if result.views:
                views = result.views[0].views_count
                # Update database
                with get_db_context() as db:
                    story = db.query(Story).filter(
                        Story.story_id == story_id
                    ).first()
                    if story:
                        story.views_count = views
                return views

        except Exception as e:
            logger.error("Failed to get story views", story_id=story_id, error=str(e))

        return None

    def _get_media_type(self, media_file: Path) -> Optional[str]:
        """Determine media type from file extension"""
        ext = media_file.suffix.lower()
        if ext in ['.jpg', '.jpeg', '.png', '.webp']:
            return "photo"
        elif ext in ['.mp4', '.mov', '.avi', '.webm']:
            return "video"
        return None

    def _get_mime_type(self, media_file: Path) -> str:
        """Get MIME type for file"""
        ext = media_file.suffix.lower()
        mime_types = {
            '.mp4': 'video/mp4',
            '.mov': 'video/quicktime',
            '.avi': 'video/x-msvideo',
            '.webm': 'video/webm',
        }
        return mime_types.get(ext, 'video/mp4')

    def _format_caption_with_mentions(
        self,
        caption: str,
        entities: List
    ) -> str:
        """Format caption with mention tags"""
        if not entities:
            return caption

        mentions = []
        for entity in entities:
            if hasattr(entity, 'username') and entity.username:
                mentions.append(f"@{entity.username}")

        if mentions:
            mention_text = " ".join(mentions)
            if caption:
                return f"{caption}\n\n{mention_text}"
            return mention_text

        return caption

    def _get_privacy_rules(self, privacy: str) -> List:
        """Get privacy rules for story"""
        if privacy == StoryPrivacy.CONTACTS:
            return [InputPrivacyValueAllowContacts()]
        elif privacy == StoryPrivacy.CLOSE_FRIENDS:
            return [InputPrivacyValueDisallowAll()]  # Simplified
        else:
            return [InputPrivacyValueAllowAll()]


# Global publisher instance
story_publisher = StoryPublisher()
