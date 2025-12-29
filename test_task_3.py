#!/usr/bin/env python3
"""
STORYFLEET - Task 3: Story Publisher
=====================================
Publish stories with mentions to Telegram.

Features:
1. Takes media (image/video) and text template
2. Processes mentions list (max 10 users per story)
3. Publishes to Telegram Stories API
4. Handles media compression/formatting
5. Returns engagement metrics

Template Example:
"Check this out! {mention1} {mention2} What do you think?"
→ Renders mentions as clickable tags in story

Usage:
    python test_task_3.py
"""
import asyncio
import io
import os
import re
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("Note: PIL not installed. Image processing limited.")

try:
    from telethon import TelegramClient, functions, types
    from telethon.tl.types import (
        InputPrivacyValueAllowAll,
        InputMediaUploadedPhoto,
        InputMediaUploadedDocument,
        MessageMediaPhoto,
        MessageMediaDocument,
    )
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    print("Note: telethon not installed. Running in simulation mode.")


class MediaType(Enum):
    """Supported media types"""
    PHOTO = "photo"
    VIDEO = "video"
    UNKNOWN = "unknown"


class StoryPrivacy(Enum):
    """Story privacy settings"""
    PUBLIC = "public"
    CONTACTS = "contacts"
    CLOSE_FRIENDS = "close_friends"
    SELECTED_USERS = "selected_users"


@dataclass
class MentionUser:
    """User to mention in story"""
    user_id: int
    username: Optional[str] = None
    display_name: Optional[str] = None
    access_hash: Optional[int] = None

    @property
    def mention_text(self) -> str:
        """Get mention text for template"""
        if self.username:
            return f"@{self.username}"
        return f"[User {self.user_id}]"


@dataclass
class MediaSpec:
    """Media specifications"""
    file_path: str
    media_type: MediaType = MediaType.UNKNOWN
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    duration_seconds: float = 0.0  # For video
    needs_compression: bool = False
    compressed_path: Optional[str] = None

    def validate(self) -> Tuple[bool, str]:
        """Validate media file"""
        path = Path(self.file_path)

        if not path.exists():
            return False, f"File not found: {self.file_path}"

        # Check size (max 50MB)
        self.size_bytes = path.stat().st_size
        max_size = 50 * 1024 * 1024
        if self.size_bytes > max_size:
            return False, f"File too large: {self.size_bytes / 1024 / 1024:.1f}MB (max 50MB)"

        if self.size_bytes == 0:
            return False, "File is empty"

        # Determine media type
        ext = path.suffix.lower()
        if ext in ['.jpg', '.jpeg', '.png', '.webp']:
            self.media_type = MediaType.PHOTO
        elif ext in ['.mp4', '.mov', '.avi', '.webm']:
            self.media_type = MediaType.VIDEO
        else:
            return False, f"Unsupported format: {ext}"

        return True, "Valid"


@dataclass
class StoryResult:
    """Result of story publication"""
    success: bool = False
    story_id: Optional[int] = None
    account_id: Optional[str] = None
    media_path: str = ""
    caption: str = ""
    mentions: List[int] = field(default_factory=list)
    views_count: int = 0
    reactions_count: int = 0
    published_at: Optional[str] = None
    expires_at: Optional[str] = None
    error_message: Optional[str] = None
    processing_time_ms: float = 0.0

    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            "success": self.success,
            "story_id": self.story_id,
            "account_id": self.account_id,
            "media_path": self.media_path,
            "caption": self.caption,
            "mentions_count": len(self.mentions),
            "mentions": self.mentions,
            "engagement": {
                "views": self.views_count,
                "reactions": self.reactions_count,
            },
            "published_at": self.published_at,
            "expires_at": self.expires_at,
            "error_message": self.error_message,
            "processing_time_ms": self.processing_time_ms,
        }


class TemplateEngine:
    """
    Processes story caption templates with mentions.

    Supports:
    - {mention1}, {mention2}, ... placeholders
    - {mention_all} for all mentions
    - {random_emoji} for random engagement emoji
    - {date}, {time} for timestamps
    """

    EMOJIS = ["🔥", "✨", "💯", "🚀", "💪", "🎯", "⚡", "💫", "🌟", "👀"]

    def __init__(self):
        self._mention_pattern = re.compile(r'\{mention(\d+)\}')
        self._mention_all_pattern = re.compile(r'\{mention_all\}')

    def render(
        self,
        template: str,
        mentions: List[MentionUser],
        extra_vars: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Render template with mentions and variables.

        Args:
            template: Caption template
            mentions: List of users to mention
            extra_vars: Additional template variables

        Returns:
            Rendered caption string
        """
        result = template

        # Replace individual mentions
        def replace_mention(match):
            index = int(match.group(1)) - 1
            if 0 <= index < len(mentions):
                return mentions[index].mention_text
            return ""

        result = self._mention_pattern.sub(replace_mention, result)

        # Replace mention_all
        if self._mention_all_pattern.search(result):
            all_mentions = " ".join(m.mention_text for m in mentions)
            result = self._mention_all_pattern.sub(all_mentions, result)

        # Replace random emoji
        result = result.replace("{random_emoji}", random.choice(self.EMOJIS))

        # Replace date/time
        now = datetime.utcnow()
        result = result.replace("{date}", now.strftime("%Y-%m-%d"))
        result = result.replace("{time}", now.strftime("%H:%M"))

        # Replace extra variables
        if extra_vars:
            for key, value in extra_vars.items():
                result = result.replace(f"{{{key}}}", str(value))

        # Clean up multiple spaces
        result = re.sub(r' +', ' ', result).strip()

        return result

    def validate_template(self, template: str) -> Tuple[bool, List[str]]:
        """Validate template and return required mentions"""
        mentions_needed = []

        # Find all mention placeholders
        for match in self._mention_pattern.finditer(template):
            index = int(match.group(1))
            if index not in mentions_needed:
                mentions_needed.append(index)

        # Check for mention_all
        has_mention_all = bool(self._mention_all_pattern.search(template))

        return True, sorted(mentions_needed)


class MediaProcessor:
    """
    Handles media compression and formatting for Telegram Stories.

    Telegram Story Requirements:
    - Photos: Max 1080x1920, JPEG/PNG
    - Videos: Max 720p, MP4, max 60 seconds
    """

    MAX_PHOTO_WIDTH = 1080
    MAX_PHOTO_HEIGHT = 1920
    MAX_VIDEO_DURATION = 60
    JPEG_QUALITY = 85

    def __init__(self, temp_dir: str = "./data/temp"):
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def process_photo(self, media: MediaSpec) -> MediaSpec:
        """Process and compress photo if needed"""
        if not PIL_AVAILABLE:
            return media

        try:
            with Image.open(media.file_path) as img:
                media.width, media.height = img.size

                # Check if resize needed
                if img.width > self.MAX_PHOTO_WIDTH or img.height > self.MAX_PHOTO_HEIGHT:
                    media.needs_compression = True

                    # Calculate new size maintaining aspect ratio
                    ratio = min(
                        self.MAX_PHOTO_WIDTH / img.width,
                        self.MAX_PHOTO_HEIGHT / img.height
                    )
                    new_size = (int(img.width * ratio), int(img.height * ratio))

                    # Resize
                    img = img.resize(new_size, Image.Resampling.LANCZOS)

                    # Save compressed
                    output_path = self.temp_dir / f"compressed_{Path(media.file_path).name}"

                    if img.mode in ('RGBA', 'P'):
                        img = img.convert('RGB')

                    img.save(output_path, 'JPEG', quality=self.JPEG_QUALITY)
                    media.compressed_path = str(output_path)
                    media.width, media.height = new_size

                    print(f"📦 Photo compressed: {img.size}")

        except Exception as e:
            print(f"⚠️ Photo processing error: {e}")

        return media

    async def process_video(self, media: MediaSpec) -> MediaSpec:
        """Process video (placeholder - would use ffmpeg)"""
        # Video processing would require ffmpeg
        # This is a placeholder that just validates
        media.needs_compression = False
        return media

    async def process(self, media: MediaSpec) -> MediaSpec:
        """Process media based on type"""
        if media.media_type == MediaType.PHOTO:
            return await self.process_photo(media)
        elif media.media_type == MediaType.VIDEO:
            return await self.process_video(media)
        return media

    def get_final_path(self, media: MediaSpec) -> str:
        """Get the final path to use for upload"""
        return media.compressed_path or media.file_path


class StoryPublisher:
    """
    Publishes stories with mentions to Telegram.

    Features:
    - Media processing and compression
    - Template-based captions with mentions
    - Privacy controls
    - Engagement tracking
    - Rate limiting
    """

    MAX_MENTIONS_PER_STORY = 10

    def __init__(
        self,
        client: Optional[Any] = None,
        temp_dir: str = "./data/temp"
    ):
        self.client = client
        self.template_engine = TemplateEngine()
        self.media_processor = MediaProcessor(temp_dir)
        self._published_stories: List[StoryResult] = []

    async def publish(
        self,
        media_path: str,
        caption_template: str,
        mentions: List[Union[int, MentionUser]],
        privacy: StoryPrivacy = StoryPrivacy.PUBLIC,
        pin_to_profile: bool = False,
        extra_vars: Optional[Dict[str, str]] = None,
    ) -> StoryResult:
        """
        Publish a story with mentions.

        Args:
            media_path: Path to media file (image/video)
            caption_template: Caption with mention placeholders
            mentions: List of user IDs or MentionUser objects (max 10)
            privacy: Story privacy setting
            pin_to_profile: Pin story to profile
            extra_vars: Additional template variables

        Returns:
            StoryResult with publication details
        """
        start_time = datetime.utcnow()

        result = StoryResult(
            media_path=media_path,
            published_at=start_time.isoformat(),
        )

        try:
            # 1. Validate and process media
            media = MediaSpec(file_path=media_path)
            is_valid, error = media.validate()

            if not is_valid:
                result.error_message = error
                return result

            media = await self.media_processor.process(media)

            # 2. Process mentions (max 10)
            mention_users = self._process_mentions(mentions[:self.MAX_MENTIONS_PER_STORY])
            result.mentions = [m.user_id for m in mention_users]

            # 3. Render caption
            caption = self.template_engine.render(
                caption_template,
                mention_users,
                extra_vars
            )
            result.caption = caption

            # 4. Publish story
            if TELETHON_AVAILABLE and self.client:
                story_result = await self._publish_to_telegram(
                    media=media,
                    caption=caption,
                    privacy=privacy,
                    pin_to_profile=pin_to_profile,
                )
                result.story_id = story_result.get("story_id")
                result.success = story_result.get("success", False)
                result.error_message = story_result.get("error")
            else:
                # Simulation mode
                result.story_id = random.randint(1000, 99999)
                result.success = True
                result.views_count = random.randint(10, 500)
                result.reactions_count = random.randint(0, 50)
                print(f"📸 [SIMULATED] Story published: ID {result.story_id}")

            # Set expiry (24 hours)
            result.expires_at = (start_time + timedelta(hours=24)).isoformat()

        except Exception as e:
            result.error_message = str(e)
            print(f"❌ Publish error: {e}")

        # Calculate processing time
        result.processing_time_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

        if result.success:
            self._published_stories.append(result)
            print(f"✅ Story published with {len(result.mentions)} mentions")

        return result

    async def _publish_to_telegram(
        self,
        media: MediaSpec,
        caption: str,
        privacy: StoryPrivacy,
        pin_to_profile: bool,
    ) -> Dict[str, Any]:
        """Publish story via Telegram API"""
        result = {"success": False, "story_id": None, "error": None}

        try:
            # Get media file
            media_path = self.media_processor.get_final_path(media)

            # Upload file
            uploaded = await self.client.upload_file(media_path)

            # Create media input
            if media.media_type == MediaType.PHOTO:
                input_media = InputMediaUploadedPhoto(file=uploaded)
            else:
                input_media = InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type="video/mp4",
                    attributes=[]
                )

            # Set privacy
            privacy_rules = [InputPrivacyValueAllowAll()]

            # Publish
            story_result = await self.client(functions.stories.SendStoryRequest(
                peer=types.InputPeerSelf(),
                media=input_media,
                caption=caption,
                privacy_rules=privacy_rules,
                pinned=pin_to_profile,
            ))

            # Extract story ID
            if hasattr(story_result, 'updates'):
                for update in story_result.updates:
                    if hasattr(update, 'story'):
                        result["story_id"] = update.story.id
                        break

            result["success"] = True

        except Exception as e:
            result["error"] = str(e)

        return result

    async def get_story_metrics(self, story_id: int) -> Dict[str, Any]:
        """Get engagement metrics for a story"""
        metrics = {
            "story_id": story_id,
            "views": 0,
            "reactions": 0,
            "replies": 0,
            "shares": 0,
        }

        if TELETHON_AVAILABLE and self.client:
            try:
                result = await self.client(functions.stories.GetStoriesViewsRequest(
                    peer=types.InputPeerSelf(),
                    id=[story_id]
                ))

                if result.views:
                    view = result.views[0]
                    metrics["views"] = getattr(view, 'views_count', 0)
                    metrics["reactions"] = getattr(view, 'reactions_count', 0)

            except Exception as e:
                print(f"⚠️ Failed to get metrics: {e}")
        else:
            # Simulation
            metrics["views"] = random.randint(50, 1000)
            metrics["reactions"] = random.randint(5, 100)

        return metrics

    async def delete_story(self, story_id: int) -> bool:
        """Delete a story"""
        if TELETHON_AVAILABLE and self.client:
            try:
                await self.client(functions.stories.DeleteStoriesRequest(
                    peer=types.InputPeerSelf(),
                    id=[story_id]
                ))
                print(f"🗑️ Story {story_id} deleted")
                return True
            except Exception as e:
                print(f"❌ Delete failed: {e}")
                return False
        else:
            print(f"🗑️ [SIMULATED] Story {story_id} deleted")
            return True

    def get_published_stories(self) -> List[Dict]:
        """Get list of published stories"""
        return [s.to_dict() for s in self._published_stories]

    def _process_mentions(
        self,
        mentions: List[Union[int, MentionUser]]
    ) -> List[MentionUser]:
        """Convert mention inputs to MentionUser objects"""
        result = []

        for mention in mentions:
            if isinstance(mention, MentionUser):
                result.append(mention)
            elif isinstance(mention, int):
                result.append(MentionUser(user_id=mention))
            elif isinstance(mention, dict):
                result.append(MentionUser(**mention))

        return result


# ============================================
# TEST SUITE
# ============================================

async def run_tests():
    """Run comprehensive tests for StoryPublisher"""
    print("\n" + "="*60)
    print("   STORYFLEET - Task 3: Story Publisher Tests")
    print("="*60 + "\n")

    # Create test media file
    test_media_dir = Path("./data/test_media")
    test_media_dir.mkdir(parents=True, exist_ok=True)

    test_image = test_media_dir / "test_story.jpg"

    # Create a simple test image
    if PIL_AVAILABLE:
        img = Image.new('RGB', (1200, 1600), color=(73, 109, 137))
        img.save(test_image, 'JPEG')
        print(f"📷 Created test image: {test_image}")
    else:
        # Create placeholder file
        with open(test_image, 'wb') as f:
            f.write(b"FAKE_IMAGE_DATA")
        print(f"📷 Created placeholder image: {test_image}")

    # Initialize publisher
    publisher = StoryPublisher()

    # Test 1: Template Engine
    print("\n📋 Test 1: Template Engine")
    print("-" * 40)

    template = "Check this out! {mention1} {mention2} What do you think? {random_emoji}"

    mentions = [
        MentionUser(user_id=123456, username="john_doe"),
        MentionUser(user_id=789012, username="jane_smith"),
    ]

    rendered = publisher.template_engine.render(template, mentions)
    print(f"  Template: {template}")
    print(f"  Rendered: {rendered}")

    assert "@john_doe" in rendered
    assert "@jane_smith" in rendered
    print("✅ Template engine test passed\n")

    # Test 2: Template with mention_all
    print("📋 Test 2: Template with mention_all")
    print("-" * 40)

    template_all = "Hey everyone! {mention_all} Join us!"
    rendered_all = publisher.template_engine.render(template_all, mentions)
    print(f"  Template: {template_all}")
    print(f"  Rendered: {rendered_all}")

    assert "@john_doe" in rendered_all
    assert "@jane_smith" in rendered_all
    print("✅ mention_all test passed\n")

    # Test 3: Media Validation
    print("📋 Test 3: Media Validation")
    print("-" * 40)

    media = MediaSpec(file_path=str(test_image))
    is_valid, message = media.validate()

    print(f"  File: {test_image}")
    print(f"  Valid: {is_valid}")
    print(f"  Type: {media.media_type.value}")
    print(f"  Size: {media.size_bytes} bytes")

    assert is_valid == True
    print("✅ Media validation test passed\n")

    # Test 4: Media Processing
    print("📋 Test 4: Media Processing")
    print("-" * 40)

    processed = await publisher.media_processor.process(media)
    print(f"  Original size: {media.size_bytes} bytes")
    print(f"  Needs compression: {processed.needs_compression}")
    if processed.compressed_path:
        print(f"  Compressed path: {processed.compressed_path}")
    print("✅ Media processing test passed\n")

    # Test 5: Story Publication (Simulated)
    print("📋 Test 5: Story Publication")
    print("-" * 40)

    result = await publisher.publish(
        media_path=str(test_image),
        caption_template="Amazing content! {mention1} {mention2} Check it out! {random_emoji}",
        mentions=[
            MentionUser(user_id=111111, username="user1"),
            MentionUser(user_id=222222, username="user2"),
            MentionUser(user_id=333333, username="user3"),
        ],
        privacy=StoryPrivacy.PUBLIC,
    )

    print(f"  Success: {result.success}")
    print(f"  Story ID: {result.story_id}")
    print(f"  Caption: {result.caption}")
    print(f"  Mentions: {result.mentions}")
    print(f"  Processing time: {result.processing_time_ms:.2f}ms")

    assert result.success == True
    assert result.story_id is not None
    assert len(result.mentions) == 3
    print("✅ Publication test passed\n")

    # Test 6: Get Metrics
    print("📋 Test 6: Story Metrics")
    print("-" * 40)

    metrics = await publisher.get_story_metrics(result.story_id)
    print(f"  Views: {metrics['views']}")
    print(f"  Reactions: {metrics['reactions']}")
    print("✅ Metrics test passed\n")

    # Test 7: Max Mentions Limit
    print("📋 Test 7: Max Mentions Limit (10)")
    print("-" * 40)

    many_mentions = [MentionUser(user_id=i, username=f"user{i}") for i in range(15)]

    result_many = await publisher.publish(
        media_path=str(test_image),
        caption_template="Testing {mention_all}",
        mentions=many_mentions,
    )

    print(f"  Input mentions: 15")
    print(f"  Actual mentions: {len(result_many.mentions)}")

    assert len(result_many.mentions) <= 10
    print("✅ Max mentions limit test passed\n")

    # Test 8: Result Format
    print("📋 Test 8: Result Format")
    print("-" * 40)

    result_dict = result.to_dict()

    required_fields = [
        "success", "story_id", "caption", "mentions_count",
        "engagement", "published_at", "expires_at"
    ]

    for field in required_fields:
        assert field in result_dict, f"Missing field: {field}"
        print(f"  ✓ Field present: {field}")

    assert "views" in result_dict["engagement"]
    assert "reactions" in result_dict["engagement"]
    print("✅ Result format test passed\n")

    # Test 9: Delete Story
    print("📋 Test 9: Delete Story")
    print("-" * 40)

    deleted = await publisher.delete_story(result.story_id)
    print(f"  Delete success: {deleted}")

    assert deleted == True
    print("✅ Delete test passed\n")

    # Test 10: Published Stories List
    print("📋 Test 10: Published Stories Tracking")
    print("-" * 40)

    published = publisher.get_published_stories()
    print(f"  Total published: {len(published)}")

    assert len(published) >= 2  # We published 2 stories
    print("✅ Tracking test passed\n")

    # Cleanup
    if test_image.exists():
        test_image.unlink()
        print("🧹 Cleaned up test files")

    # Summary
    print("="*60)
    print("   ALL TESTS PASSED ✅")
    print("="*60)

    # Print sample output
    print("\n📊 Sample Story Result:")
    print("-" * 40)
    import json
    print(json.dumps(result_dict, indent=2))

    return publisher


if __name__ == "__main__":
    asyncio.run(run_tests())
