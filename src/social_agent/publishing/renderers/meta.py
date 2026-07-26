"""Meta destination renderers — build Graph-shaped preview JSON only.

HARD SAFETY:
- Never call Meta Graph HTTP.
- Never attach OAuth / page access tokens.
- Never create media containers or publish posts.
"""
from __future__ import annotations

from typing import Any

from src.social_agent.publishing import constants as C
from src.social_agent.publishing.content import PublishContent


def _page_context(connection: dict[str, Any] | None) -> dict[str, Any]:
    connection = connection or {}
    return {
        "page_id": connection.get("selected_page_id"),
        "page_name": connection.get("selected_page_name") or connection.get("display_name"),
        "instagram_account_id": connection.get("selected_instagram_id"),
        "instagram_username": connection.get("selected_instagram_username"),
        "connection_status": connection.get("status"),
    }


def _media_items(content: PublishContent) -> list[dict[str, Any]]:
    items = []
    for img in content.images:
        items.append({"type": "image", "url": img.url, "alt": img.alt, "mime_type": img.mime_type})
    for vid in content.videos:
        items.append({"type": "video", "url": vid.url, "alt": vid.alt, "mime_type": vid.mime_type})
    return items


class MetaFacebookPageRenderer:
    destination = C.FACEBOOK_PAGE

    def render(self, content: PublishContent, *, connection: dict[str, Any] | None) -> dict[str, Any]:
        ctx = _page_context(connection)
        caption = content.caption_with_hashtags()
        media = _media_items(content)
        method = "POST"
        # Shape mirrors Graph Page feed publish, but is never sent.
        if media and all(m["type"] == "image" for m in media) and len(media) > 1:
            endpoint = f"/{ctx['page_id'] or '{page-id}'}/feed"
            payload = {
                "message": caption or None,
                "attached_media": [{"media_fbid": f"{{unpublished-photo-{i}}}"} for i, _ in enumerate(media)],
                "link": content.link,
            }
            unpublished_photos = [
                {
                    "endpoint": f"/{ctx['page_id'] or '{page-id}'}/photos",
                    "method": "POST",
                    "body": {"url": m["url"], "published": False, "caption": caption if i == 0 else None},
                }
                for i, m in enumerate(media)
            ]
        elif media and media[0]["type"] == "video":
            endpoint = f"/{ctx['page_id'] or '{page-id}'}/videos"
            payload = {"description": caption or None, "file_url": media[0]["url"]}
            unpublished_photos = []
        elif media and media[0]["type"] == "image":
            endpoint = f"/{ctx['page_id'] or '{page-id}'}/photos"
            payload = {"url": media[0]["url"], "caption": caption or None, "published": True}
            unpublished_photos = []
        else:
            endpoint = f"/{ctx['page_id'] or '{page-id}'}/feed"
            payload = {"message": caption or None, "link": content.link}
            unpublished_photos = []
        if content.cta:
            payload["call_to_action"] = {
                "type": content.cta.type,
                "value": {"link": content.cta.url or content.link},
            }
        return {
            "provider": "meta",
            "destination": self.destination,
            "graph_api_version": "v25.0",
            "http_method": method,
            "endpoint": endpoint,
            "would_send": False,
            "provider_called": False,
            "page": ctx,
            "body": {k: v for k, v in payload.items() if v is not None},
            "media": media,
            "unpublished_photo_steps": unpublished_photos,
            "notes": [
                "Dry-run preview only.",
                "No Graph HTTP request is executed.",
                "Page access tokens are intentionally omitted.",
            ],
        }


class MetaInstagramFeedRenderer:
    destination = C.INSTAGRAM_FEED

    def render(self, content: PublishContent, *, connection: dict[str, Any] | None) -> dict[str, Any]:
        ctx = _page_context(connection)
        media = _media_items(content)
        ig = ctx["instagram_account_id"] or "{instagram-business-account-id}"
        caption = content.caption_with_hashtags()
        first = media[0] if media else {"type": "image", "url": None}
        create_body = {
            "caption": caption or None,
            "image_url": first["url"] if first.get("type") == "image" else None,
            "video_url": first["url"] if first.get("type") == "video" else None,
            "media_type": "IMAGE" if first.get("type") == "image" else "VIDEO",
        }
        return {
            "provider": "meta",
            "destination": self.destination,
            "graph_api_version": "v25.0",
            "would_send": False,
            "provider_called": False,
            "page": ctx,
            "steps": [
                {
                    "step": 1,
                    "name": "create_media_container",
                    "http_method": "POST",
                    "endpoint": f"/{ig}/media",
                    "body": {k: v for k, v in create_body.items() if v is not None},
                },
                {
                    "step": 2,
                    "name": "publish_media_container",
                    "http_method": "POST",
                    "endpoint": f"/{ig}/media_publish",
                    "body": {"creation_id": "{container-id}"},
                },
            ],
            "notes": [
                "Dry-run preview only — containers are not created.",
                "No Graph HTTP request is executed.",
            ],
        }


class MetaInstagramCarouselRenderer:
    destination = C.INSTAGRAM_CAROUSEL

    def render(self, content: PublishContent, *, connection: dict[str, Any] | None) -> dict[str, Any]:
        ctx = _page_context(connection)
        media = _media_items(content)
        ig = ctx["instagram_account_id"] or "{instagram-business-account-id}"
        child_steps = []
        for i, item in enumerate(media):
            body = {
                "is_carousel_item": True,
                "media_type": "IMAGE" if item["type"] == "image" else "VIDEO",
                "image_url": item["url"] if item["type"] == "image" else None,
                "video_url": item["url"] if item["type"] == "video" else None,
            }
            child_steps.append(
                {
                    "step": i + 1,
                    "name": "create_carousel_child",
                    "http_method": "POST",
                    "endpoint": f"/{ig}/media",
                    "body": {k: v for k, v in body.items() if v is not None},
                }
            )
        parent_step_no = len(child_steps) + 1
        return {
            "provider": "meta",
            "destination": self.destination,
            "graph_api_version": "v25.0",
            "would_send": False,
            "provider_called": False,
            "page": ctx,
            "steps": child_steps
            + [
                {
                    "step": parent_step_no,
                    "name": "create_carousel_container",
                    "http_method": "POST",
                    "endpoint": f"/{ig}/media",
                    "body": {
                        "media_type": "CAROUSEL",
                        "caption": content.caption_with_hashtags() or None,
                        "children": ["{child-container-id-%d}" % (i + 1) for i in range(len(media))],
                    },
                },
                {
                    "step": parent_step_no + 1,
                    "name": "publish_carousel",
                    "http_method": "POST",
                    "endpoint": f"/{ig}/media_publish",
                    "body": {"creation_id": "{carousel-container-id}"},
                },
            ],
            "notes": [
                "Dry-run preview only — carousel containers are not created.",
                "No Graph HTTP request is executed.",
            ],
        }


class MetaInstagramStoryRenderer:
    destination = C.INSTAGRAM_STORY

    def render(self, content: PublishContent, *, connection: dict[str, Any] | None) -> dict[str, Any]:
        ctx = _page_context(connection)
        media = _media_items(content)
        ig = ctx["instagram_account_id"] or "{instagram-business-account-id}"
        first = media[0] if media else {"type": "image", "url": None}
        body = {
            "media_type": "STORIES",
            "image_url": first["url"] if first.get("type") == "image" else None,
            "video_url": first["url"] if first.get("type") == "video" else None,
        }
        return {
            "provider": "meta",
            "destination": self.destination,
            "graph_api_version": "v25.0",
            "would_send": False,
            "provider_called": False,
            "page": ctx,
            "steps": [
                {
                    "step": 1,
                    "name": "create_story_container",
                    "http_method": "POST",
                    "endpoint": f"/{ig}/media",
                    "body": {k: v for k, v in body.items() if v is not None},
                },
                {
                    "step": 2,
                    "name": "publish_story",
                    "http_method": "POST",
                    "endpoint": f"/{ig}/media_publish",
                    "body": {"creation_id": "{story-container-id}"},
                },
            ],
            "notes": [
                "Dry-run preview only — story containers are not created.",
                "No Graph HTTP request is executed.",
                "This is unrelated to Telegram Storyfleet Story mutations.",
            ],
        }


RENDERERS = {
    C.FACEBOOK_PAGE: MetaFacebookPageRenderer(),
    C.INSTAGRAM_FEED: MetaInstagramFeedRenderer(),
    C.INSTAGRAM_CAROUSEL: MetaInstagramCarouselRenderer(),
    C.INSTAGRAM_STORY: MetaInstagramStoryRenderer(),
}


def get_renderer(destination: str):
    return RENDERERS.get((destination or "").strip().lower())
