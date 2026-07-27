"""Social copilot helpers — local, brand-aware, never auto-publish."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.brand_store import brand_context_for_ai, search_knowledge
from src.social_agent.platforms import DEFAULT_VARIANT_PLATFORMS, PLATFORM_LIMITS


def _brand_banned(db: Session) -> list[str]:
    hits = search_knowledge(db, query="banned")
    words: list[str] = []
    for h in hits.get("hits") or []:
        if h.get("category") == "banned":
            words.extend([w.strip() for w in re.split(r"[,;]", h.get("value") or "") if w.strip()])
    return words


def rewrite_content(text: str, *, mode: str = "improve") -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "empty_text"}
    mode_l = (mode or "improve").lower()
    if mode_l == "shorten":
        out = body if len(body) <= 180 else (body[:177].rstrip() + "...")
    elif mode_l == "grammar":
        out = body[0].upper() + body[1:] if body else body
        out = re.sub(r"\s+", " ", out).strip()
        if out and out[-1] not in ".!?":
            out += "."
    elif mode_l == "engagement":
        out = body.rstrip(".") + ". What would you like to exchange today?"
    else:
        out = body
        if not out.endswith((".", "!", "?")):
            out += "."
    return {"ok": True, "mode": mode_l, "text": out, "ai_used": False, "provider_called": False}


def suggest_hashtags(db: Session, text: str = "") -> dict[str, Any]:
    brand = search_knowledge(db, query="hashtag")
    approved: list[str] = []
    for h in brand.get("hits") or []:
        if h.get("category") == "hashtags":
            approved.extend(re.findall(r"#?\w+", h.get("value") or ""))
    cleaned = []
    for tag in approved:
        t = tag if tag.startswith("#") else f"#{tag}"
        if t.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(t)
    # Light contextual suggestions from text tokens — never fabricate metrics.
    tokens = re.findall(r"[A-Za-z]{4,}", text or "")
    for tok in tokens[:3]:
        cand = f"#{tok.capitalize()}"
        if cand.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(cand)
    return {"ok": True, "hashtags": cleaned[:12], "source": "brand+local", "ai_used": False}


def compliance_check(db: Session, text: str) -> dict[str, Any]:
    body = text or ""
    lower = body.lower()
    banned = _brand_banned(db)
    hits = [w for w in banned if w.lower() in lower]
    legal = search_knowledge(db, query="legal footer")
    return {
        "ok": True,
        "compliant": len(hits) == 0,
        "banned_hits": hits,
        "recommendations": [
            "Use approved CTAs only.",
            "Avoid guaranteed-return language.",
            ((legal.get("hits") or [{}])[0].get("value") or "Include legal footer when promoting rates."),
        ],
        "ai_used": False,
    }


def image_prompt(brief: str) -> dict[str, Any]:
    b = (brief or "Exswaping brand visual").strip()
    prompt = (
        f"Clean fintech social graphic for Exswaping: {b}. "
        "Minimal layout, high contrast, no logos of other brands, no fake charts, no guaranteed-return claims."
    )
    return {"ok": True, "prompt": prompt, "ai_used": False, "provider_called": False}


def engagement_hints(text: str, platform: str = "facebook") -> dict[str, Any]:
    limit = PLATFORM_LIMITS.get(platform, 5000)
    length = len(text or "")
    hints = []
    if length == 0:
        hints.append("Add a clear opening line.")
    if length > limit:
        hints.append(f"Shorten for {platform} (limit {limit}).")
    if "?" not in (text or ""):
        hints.append("Consider one soft question to invite replies.")
    if "#" not in (text or ""):
        hints.append("Add approved hashtags when appropriate.")
    return {
        "ok": True,
        "platform": platform,
        "char_count": length,
        "limit": limit,
        "hints": hints,
        "predicted_engagement": None,
        "message": "Engagement prediction requires certified analytics. Hints only.",
        "ai_used": False,
    }


def calendar_suggestions(brief: str) -> dict[str, Any]:
    """Suggest a content calendar outline — does not schedule or publish."""
    theme = (brief or "weekly Exswaping updates").strip()
    days = ["Monday", "Wednesday", "Friday"]
    slots = [
        {"day": d, "theme": f"{theme} — {d} angle", "platforms": list(DEFAULT_VARIANT_PLATFORMS[:3])}
        for d in days
    ]
    return {
        "ok": True,
        "suggestions": slots,
        "scheduled": False,
        "published": False,
        "message": "Suggestion only. Use Calendar to queue drafts; live publishing remains disabled.",
    }


def summarize_comments_stub() -> dict[str, Any]:
    return {
        "ok": True,
        "summary": None,
        "message": "No comments available to summarize.",
        "provider_called": False,
    }


def copilot_capabilities() -> list[str]:
    return [
        "generate_campaign",
        "rewrite_content",
        "improve_engagement",
        "suggest_hashtags",
        "generate_variants",
        "generate_image_prompts",
        "review_grammar",
        "check_compliance",
        "summarize_comments",
        "predict_engagement",
        "generate_content_calendar",
    ]


def assist(db: Session, *, intent: str, text: str = "", platform: str = "facebook") -> dict[str, Any]:
    intent_l = (intent or "").strip().lower()
    brand = brand_context_for_ai(db)
    if intent_l in {"rewrite", "improve"}:
        out = rewrite_content(text, mode="improve")
    elif intent_l in {"shorten"}:
        out = rewrite_content(text, mode="shorten")
    elif intent_l in {"grammar"}:
        out = rewrite_content(text, mode="grammar")
    elif intent_l in {"engagement"}:
        out = engagement_hints(text, platform=platform)
    elif intent_l in {"hashtags"}:
        out = suggest_hashtags(db, text)
    elif intent_l in {"image_prompt", "image"}:
        out = image_prompt(text)
    elif intent_l in {"compliance", "grammar_compliance"}:
        out = compliance_check(db, text)
    elif intent_l in {"calendar"}:
        out = calendar_suggestions(text)
    elif intent_l in {"comments"}:
        out = summarize_comments_stub()
    elif intent_l in {"predict"}:
        out = engagement_hints(text, platform=platform)
    else:
        out = {
            "ok": True,
            "capabilities": copilot_capabilities(),
            "message": "Never publishes automatically. Ask to rewrite, hashtag, compliance-check, or draft variants.",
        }
    out["brand_context_attached"] = True
    out["brand_categories"] = sorted((brand.get("brand") or {}).keys())
    out["auto_publish"] = False
    return out
