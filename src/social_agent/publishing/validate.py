"""Destination validators — never silently mutate content."""
from __future__ import annotations

import re
from typing import Any

from src.social_agent.publishing import constants as C
from src.social_agent.publishing.content import PublishContent

_HASHTAG_RE = re.compile(r"^#?[A-Za-z0-9_]{1,100}$")
_URL_RE = re.compile(r"^https?://", re.I)


def _err(code: str, message: str, *, field: str | None = None) -> dict[str, Any]:
    out = {"code": code, "message": message}
    if field:
        out["field"] = field
    return out


def validate_hashtags(content: PublishContent) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for i, tag in enumerate(content.hashtags or []):
        raw = str(tag or "").strip()
        if not raw or not _HASHTAG_RE.match(raw):
            errors.append(_err("invalid_hashtag", f"Invalid hashtag at index {i}: {raw!r}", field="hashtags"))
    if len(content.hashtags or []) > C.INSTAGRAM_HASHTAG_LIMIT:
        errors.append(
            _err(
                "hashtag_limit",
                f"At most {C.INSTAGRAM_HASHTAG_LIMIT} hashtags allowed for Instagram destinations",
                field="hashtags",
            )
        )
    return errors


def validate_destination(destination: str, content: PublishContent) -> dict[str, Any]:
    dest = (destination or "").strip().lower()
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    caption = content.caption_with_hashtags()
    image_count = len(content.images or [])
    video_count = len(content.videos or [])

    if dest not in C.SUPPORTED_DESTINATIONS:
        return {
            "destination": dest or None,
            "ok": False,
            "errors": [_err("unsupported_destination", f"Unsupported destination: {destination!r}")],
            "warnings": [],
        }

    errors.extend(validate_hashtags(content))

    if dest == C.FACEBOOK_PAGE:
        if not caption and image_count == 0 and video_count == 0 and not content.link:
            errors.append(_err("empty_content", "Facebook Page requires text, media, or a link"))
        if len(caption) > C.FACEBOOK_MESSAGE_LIMIT:
            errors.append(
                _err(
                    "text_too_long",
                    f"Facebook message exceeds {C.FACEBOOK_MESSAGE_LIMIT} characters",
                    field="text",
                )
            )
        if content.cta:
            if content.cta.type not in C.ALLOWED_CTA_TYPES:
                errors.append(_err("unsupported_cta", f"Unsupported CTA type: {content.cta.type}", field="cta"))
            if not content.link and not content.cta.url:
                errors.append(_err("cta_requires_url", "CTA requires a link URL", field="cta"))
        if content.link and not _URL_RE.match(content.link):
            errors.append(_err("invalid_link", "Link must be http(s)", field="link"))
        if image_count > 10:
            errors.append(_err("too_many_images", "Facebook preview supports at most 10 images", field="images"))
        if video_count > 1:
            errors.append(_err("too_many_videos", "Facebook preview supports at most 1 video", field="videos"))
        if image_count and video_count:
            errors.append(_err("unsupported_combination", "Do not mix images and video in one Facebook payload"))

    elif dest == C.INSTAGRAM_FEED:
        if image_count == 0 and video_count == 0:
            errors.append(_err("media_required", "Instagram Feed requires one image or one video", field="images"))
        if image_count > C.INSTAGRAM_FEED_IMAGE_MAX:
            errors.append(_err("too_many_images", "Instagram Feed allows exactly one image (use carousel for more)", field="images"))
        if video_count > C.INSTAGRAM_FEED_VIDEO_MAX:
            errors.append(_err("too_many_videos", "Instagram Feed allows exactly one video", field="videos"))
        if image_count and video_count:
            errors.append(_err("unsupported_combination", "Instagram Feed cannot mix image and video"))
        if len(caption) > C.INSTAGRAM_CAPTION_LIMIT:
            errors.append(
                _err(
                    "caption_too_long",
                    f"Instagram caption exceeds {C.INSTAGRAM_CAPTION_LIMIT} characters",
                    field="text",
                )
            )
        if content.link:
            warnings.append(_err("link_ignored", "Instagram Feed ignores external link fields in captions"))
        if content.cta:
            errors.append(_err("unsupported_cta", "Instagram Feed does not support Facebook CTA objects", field="cta"))

    elif dest == C.INSTAGRAM_CAROUSEL:
        total = image_count + video_count
        if total < C.INSTAGRAM_CAROUSEL_MIN:
            errors.append(
                _err(
                    "carousel_too_small",
                    f"Instagram Carousel requires at least {C.INSTAGRAM_CAROUSEL_MIN} media items",
                    field="images",
                )
            )
        if total > C.INSTAGRAM_CAROUSEL_MAX:
            errors.append(
                _err(
                    "carousel_too_large",
                    f"Instagram Carousel supports at most {C.INSTAGRAM_CAROUSEL_MAX} media items",
                    field="images",
                )
            )
        if len(caption) > C.INSTAGRAM_CAPTION_LIMIT:
            errors.append(
                _err(
                    "caption_too_long",
                    f"Instagram caption exceeds {C.INSTAGRAM_CAPTION_LIMIT} characters",
                    field="text",
                )
            )
        if content.cta:
            errors.append(_err("unsupported_cta", "Instagram Carousel does not support CTA objects", field="cta"))
        if content.link:
            warnings.append(_err("link_ignored", "Instagram Carousel ignores external link fields"))

    elif dest == C.INSTAGRAM_STORY:
        total = image_count + video_count
        if total == 0:
            errors.append(_err("media_required", "Instagram Story requires one image or one video"))
        if image_count > C.INSTAGRAM_STORY_IMAGE_MAX:
            errors.append(_err("too_many_images", "Instagram Story allows one image", field="images"))
        if video_count > C.INSTAGRAM_STORY_VIDEO_MAX:
            errors.append(_err("too_many_videos", "Instagram Story allows one video", field="videos"))
        if image_count and video_count:
            errors.append(_err("unsupported_combination", "Instagram Story cannot mix image and video"))
        if caption:
            warnings.append(_err("caption_not_used", "Instagram Story preview does not publish feed captions"))
        if content.cta:
            errors.append(_err("unsupported_cta", "Instagram Story dry-run does not support CTA objects", field="cta"))
        if content.hashtags:
            warnings.append(_err("hashtags_not_used", "Hashtags are not applied to Story dry-run payloads"))

    for media in list(content.images) + list(content.videos):
        if not media.url or not str(media.url).strip():
            errors.append(_err("invalid_media_url", "Media item is missing url", field="media"))
        elif not _URL_RE.match(str(media.url).strip()) and not str(media.url).startswith("/"):
            # Allow relative asset paths for future media library; flag remote-looking invalids.
            if "://" in str(media.url):
                errors.append(_err("invalid_media_url", f"Unsupported media URL scheme: {media.url!r}", field="media"))

    return {
        "destination": dest,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "caption_length": len(caption),
            "image_count": image_count,
            "video_count": video_count,
            "hashtag_count": len(content.hashtags or []),
        },
    }


def validate_request(*, destinations: list[str], content: PublishContent) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if not destinations:
        errors.append(_err("missing_destination", "At least one destination is required", field="destinations"))
    per_dest = [validate_destination(d, content) for d in destinations]
    ok = not errors and all(d["ok"] for d in per_dest)
    return {
        "ok": ok,
        "errors": errors,
        "destinations": per_dest,
    }
