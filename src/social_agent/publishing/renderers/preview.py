"""Preview-only platform renderers for Content Studio.

Meta destinations reuse shapes from the canonical Meta dry-run renderers
without attaching tokens or calling Graph. Other platforms return UI preview
payloads only — never provider HTTP.
"""
from __future__ import annotations

from typing import Any

from src.social_agent.publishing.content import PublishContent
from src.social_agent.publishing.renderers import meta as meta_renderers
from src.social_agent.platforms import PLATFORM_LIMITS, STUDIO_TO_META_DESTINATION


def _base(platform: str, content: PublishContent) -> dict[str, Any]:
    caption = content.caption_with_hashtags()
    return {
        "platform": platform,
        "would_send": False,
        "provider_called": False,
        "char_count": len(caption or ""),
        "char_limit": PLATFORM_LIMITS.get(platform, 5000),
        "body": {"message": caption or ""},
        "notes": [
            "Studio preview only.",
            "No provider HTTP request is executed.",
            "Tokens are intentionally omitted.",
        ],
    }


def render_studio_preview(
    platform: str,
    content: PublishContent,
    *,
    connection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plat = (platform or "").strip().lower()
    meta_dest = STUDIO_TO_META_DESTINATION.get(plat)
    if meta_dest == "facebook_page":
        payload = meta_renderers.MetaFacebookPageRenderer().render(content, connection=connection)
        payload["studio_platform"] = plat
        payload["would_send"] = False
        payload["provider_called"] = False
        return payload
    if meta_dest == "instagram_feed":
        payload = meta_renderers.MetaInstagramFeedRenderer().render(content, connection=connection)
        payload["studio_platform"] = plat
        payload["would_send"] = False
        payload["provider_called"] = False
        return payload

    out = _base(plat, content)
    if plat == "telegram":
        out.update(
            {
                "provider": "telegram",
                "destination": "telegram_channel",
                "channel": "{channel}",
                "parse_mode": "HTML",
            }
        )
    elif plat == "x":
        out.update({"provider": "x", "destination": "x_post", "endpoint": "/2/tweets"})
    elif plat == "linkedin":
        out.update({"provider": "linkedin", "destination": "linkedin_ugc", "endpoint": "/v2/ugcPosts"})
    elif plat == "tiktok":
        out.update(
            {
                "provider": "tiktok",
                "destination": "tiktok_caption",
                "notes": out["notes"] + ["Caption-only preview; video upload not enabled."],
            }
        )
    elif plat == "youtube_community":
        out.update({"provider": "youtube", "destination": "youtube_community_post"})
    elif plat == "discord":
        out.update({"provider": "discord", "destination": "discord_webhook_message"})
    else:
        out["error"] = "unsupported_platform"
    return out
