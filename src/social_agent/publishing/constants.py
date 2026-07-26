"""Publishing destination and limit constants (dry-run)."""
from __future__ import annotations

FACEBOOK_PAGE = "facebook_page"
INSTAGRAM_FEED = "instagram_feed"
INSTAGRAM_CAROUSEL = "instagram_carousel"
INSTAGRAM_STORY = "instagram_story"

META_DESTINATIONS = (
    FACEBOOK_PAGE,
    INSTAGRAM_FEED,
    INSTAGRAM_CAROUSEL,
    INSTAGRAM_STORY,
)

SUPPORTED_DESTINATIONS = META_DESTINATIONS

# Official-ish practical limits used for validation (preview only).
FACEBOOK_MESSAGE_LIMIT = 63206
INSTAGRAM_CAPTION_LIMIT = 2200
INSTAGRAM_HASHTAG_LIMIT = 30
INSTAGRAM_CAROUSEL_MIN = 2
INSTAGRAM_CAROUSEL_MAX = 10
INSTAGRAM_STORY_IMAGE_MAX = 1
INSTAGRAM_STORY_VIDEO_MAX = 1
INSTAGRAM_FEED_IMAGE_MAX = 1
INSTAGRAM_FEED_VIDEO_MAX = 1

ALLOWED_CTA_TYPES = frozenset(
    {
        "LEARN_MORE",
        "SHOP_NOW",
        "SIGN_UP",
        "CONTACT_US",
        "BOOK_NOW",
        "GET_OFFER",
        "WATCH_MORE",
        "APPLY_NOW",
        "CALL_NOW",
        "MESSAGE_PAGE",
    }
)

HASHTAG_PATTERN = r"(^|\s)#([A-Za-z0-9_]{1,100})\b"
