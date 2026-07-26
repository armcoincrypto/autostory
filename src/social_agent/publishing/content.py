"""Normalized content model for dry-run publishing."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MediaAsset:
    url: str
    kind: str = "image"  # image | video
    alt: str | None = None
    mime_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CallToAction:
    type: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class PublishContent:
    text: str = ""
    hashtags: list[str] = field(default_factory=list)
    images: list[MediaAsset] = field(default_factory=list)
    videos: list[MediaAsset] = field(default_factory=list)
    link: str | None = None
    cta: CallToAction | None = None
    language: str = "EN"
    brand_voice: str | None = None
    title: str | None = None
    content_id: int | None = None

    def caption_with_hashtags(self) -> str:
        base = (self.text or "").strip()
        tags = []
        for raw in self.hashtags or []:
            tag = str(raw or "").strip()
            if not tag:
                continue
            if not tag.startswith("#"):
                tag = "#" + tag.lstrip("#")
            tags.append(tag)
        if not tags:
            return base
        if not base:
            return " ".join(tags)
        return f"{base}\n\n{' '.join(tags)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "hashtags": list(self.hashtags or []),
            "images": [m.to_dict() for m in self.images],
            "videos": [m.to_dict() for m in self.videos],
            "link": self.link,
            "cta": self.cta.to_dict() if self.cta else None,
            "language": self.language,
            "brand_voice": self.brand_voice,
            "title": self.title,
            "content_id": self.content_id,
            "caption_preview": self.caption_with_hashtags(),
        }


def parse_content(raw: dict[str, Any] | None) -> PublishContent:
    raw = raw or {}
    images = []
    for item in raw.get("images") or []:
        if isinstance(item, str):
            images.append(MediaAsset(url=item, kind="image"))
        elif isinstance(item, dict) and item.get("url"):
            images.append(
                MediaAsset(
                    url=str(item["url"]),
                    kind=str(item.get("kind") or "image"),
                    alt=item.get("alt"),
                    mime_type=item.get("mime_type"),
                )
            )
    videos = []
    for item in raw.get("videos") or []:
        if isinstance(item, str):
            videos.append(MediaAsset(url=item, kind="video"))
        elif isinstance(item, dict) and item.get("url"):
            videos.append(
                MediaAsset(
                    url=str(item["url"]),
                    kind="video",
                    alt=item.get("alt"),
                    mime_type=item.get("mime_type"),
                )
            )
    cta = None
    raw_cta = raw.get("cta")
    if isinstance(raw_cta, dict) and raw_cta.get("type"):
        cta = CallToAction(type=str(raw_cta["type"]).upper(), url=raw_cta.get("url"))
    hashtags = raw.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [h for h in hashtags.replace(",", " ").split() if h]
    return PublishContent(
        text=str(raw.get("text") or raw.get("body") or raw.get("caption") or ""),
        hashtags=[str(h) for h in hashtags],
        images=images,
        videos=videos,
        link=(str(raw["link"]).strip() if raw.get("link") else None),
        cta=cta,
        language=str(raw.get("language") or "EN").upper(),
        brand_voice=(str(raw["brand_voice"]) if raw.get("brand_voice") else None),
        title=(str(raw["title"]) if raw.get("title") else None),
        content_id=int(raw["content_id"]) if raw.get("content_id") is not None else None,
    )
