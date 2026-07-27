"""Brand knowledge store — persisted rules consumed by Content Studio and AI."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.models import SocialBrandKnowledge

SEED: list[dict[str, str]] = [
    {"category": "voice", "key": "primary", "title": "Brand voice", "value": "Clear, trustworthy, calm. No hype."},
    {"category": "tone", "key": "default", "title": "Default tone", "value": "Professional and helpful; avoid slang that ages poorly."},
    {"category": "emoji", "key": "rules", "title": "Emoji rules", "value": "Use sparingly. Prefer none in compliance or rate posts."},
    {"category": "hashtags", "key": "approved", "title": "Approved hashtags", "value": "#Exswaping #USDT"},
    {"category": "cta", "key": "rules", "title": "CTA rules", "value": "Use official Exswaping destination links only. No urgency scams."},
    {"category": "banned", "key": "words", "title": "Banned words", "value": "guaranteed returns, risk-free, get rich, no KYC, bypass AML"},
    {"category": "facts", "key": "company", "title": "Company facts", "value": "Exswaping is a crypto exchange brand operated under Kobbex / Zellotex properties."},
    {"category": "urls", "key": "primary", "title": "Primary URL", "value": "https://ex.zellotex.com"},
    {"category": "social_links", "key": "facebook", "title": "Facebook Page", "value": "Exswaping"},
    {"category": "social_links", "key": "instagram", "title": "Instagram", "value": "@exswaping"},
    {"category": "legal", "key": "footer", "title": "Legal footer", "value": "Not financial advice. Local regulations and KYC/AML apply."},
]


def ensure_seed(db: Session, *, workspace_id: str = "default") -> int:
    created = 0
    for item in SEED:
        exists = (
            db.query(SocialBrandKnowledge)
            .filter(
                SocialBrandKnowledge.workspace_id == workspace_id,
                SocialBrandKnowledge.category == item["category"],
                SocialBrandKnowledge.key == item["key"],
            )
            .first()
        )
        if exists:
            continue
        db.add(
            SocialBrandKnowledge(
                workspace_id=workspace_id,
                category=item["category"],
                key=item["key"],
                title=item["title"],
                value=item["value"],
                updated_by="system_seed",
            )
        )
        created += 1
    if created:
        db.flush()
    return created


def list_knowledge(db: Session, *, workspace_id: str = "default") -> dict[str, Any]:
    ensure_seed(db, workspace_id=workspace_id)
    rows = (
        db.query(SocialBrandKnowledge)
        .filter(SocialBrandKnowledge.workspace_id == workspace_id)
        .order_by(SocialBrandKnowledge.category.asc(), SocialBrandKnowledge.key.asc())
        .all()
    )
    return {
        "ok": True,
        "items": [
            {
                "id": r.id,
                "category": r.category,
                "key": r.key,
                "title": r.title,
                "value": r.value,
                "updated_by": r.updated_by,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


def search_knowledge(db: Session, *, query: str = "", workspace_id: str = "default") -> dict[str, Any]:
    ensure_seed(db, workspace_id=workspace_id)
    q = (query or "").strip().lower()
    rows = (
        db.query(SocialBrandKnowledge)
        .filter(SocialBrandKnowledge.workspace_id == workspace_id)
        .order_by(SocialBrandKnowledge.category.asc())
        .all()
    )
    hits = []
    for r in rows:
        blob = f"{r.category} {r.key} {r.title} {r.value}".lower()
        if not q or q in blob:
            hits.append(
                {
                    "id": r.id,
                    "category": r.category,
                    "key": r.key,
                    "title": r.title,
                    "text": r.value,
                    "value": r.value,
                }
            )
    return {"ok": True, "hits": hits, "source": "social_brand_knowledge"}


def upsert_knowledge(
    db: Session,
    *,
    actor: str | None,
    category: str,
    key: str,
    title: str,
    value: str,
    workspace_id: str = "default",
) -> dict[str, Any]:
    cat = (category or "").strip().lower()[:64]
    k = (key or "").strip().lower()[:128]
    if not cat or not k:
        return {"ok": False, "error": "category_and_key_required"}
    row = (
        db.query(SocialBrandKnowledge)
        .filter(
            SocialBrandKnowledge.workspace_id == workspace_id,
            SocialBrandKnowledge.category == cat,
            SocialBrandKnowledge.key == k,
        )
        .first()
    )
    if row is None:
        row = SocialBrandKnowledge(
            workspace_id=workspace_id,
            category=cat,
            key=k,
            title=(title or k)[:255],
            value=value or "",
            updated_by=actor,
        )
        db.add(row)
    else:
        row.title = (title or row.title or k)[:255]
        row.value = value or ""
        row.updated_by = actor
    db.flush()
    return {
        "ok": True,
        "item": {
            "id": row.id,
            "category": row.category,
            "key": row.key,
            "title": row.title,
            "value": row.value,
        },
    }


def brand_context_for_ai(db: Session, *, workspace_id: str = "default") -> dict[str, Any]:
    """Compact brand packet automatically injected into assistant replies."""
    listed = list_knowledge(db, workspace_id=workspace_id)
    by_cat: dict[str, list[dict[str, str]]] = {}
    for item in listed.get("items") or []:
        by_cat.setdefault(item["category"], []).append(
            {"key": item["key"], "title": item["title"], "value": item["value"]}
        )
    return {"ok": True, "brand": by_cat}
