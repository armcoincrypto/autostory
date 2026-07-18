"""Controlled Story mention plan helpers (caption @username + entities).

Intended product contract
-------------------------
StoryFleet Story "mentions" are **caption text mentions** of the form ``@username``,
optionally accompanied by Telegram ``MessageEntityMention`` caption entities on
``SendStoryRequest``.

They are **not**:
- Story media-area user stickers (Telethon 1.43.2 has no user InputMediaArea type)
- Analytics-only placeholders
- Channel attribution posts

A Dry Run candidate is only "applied" after the publishing account can resolve the
peer (prefer username) and the caption/entities are included in SendStoryRequest.
Selection alone does not imply Telegram received a mention.
"""
from __future__ import annotations

from typing import Any


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def normalize_mention_candidate(row: Any) -> dict[str, Any] | None:
    """Normalize a candidate dict / user id into a stable mention plan row."""
    if row is None:
        return None
    if isinstance(row, (int, str)) and str(row).strip().lstrip("-").isdigit():
        return {
            "user_id": int(row),
            "username": None,
            "source_chat_id": None,
            "source_chat_title": None,
        }
    if not isinstance(row, dict):
        return None
    uid = row.get("user_id") or row.get("peer_id") or row.get("id")
    if uid in (None, "", "null"):
        return None
    username = row.get("username")
    if isinstance(username, str):
        username = username.strip().lstrip("@") or None
    else:
        username = None
    return {
        "user_id": int(uid),
        "username": username,
        "source_chat_id": row.get("source_chat_id"),
        "source_chat_title": row.get("source_chat_title"),
        "times_mentioned": row.get("times_mentioned"),
    }


def normalize_mention_plan(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in rows or []:
        item = normalize_mention_candidate(raw)
        if not item:
            continue
        uid = int(item["user_id"])
        if uid in seen:
            continue
        seen.add(uid)
        out.append(item)
    return out


def extract_approved_mention_plan(payload: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Return explicit approved plan from live/dry payload, or None if absent."""
    payload = payload or {}
    raw = payload.get("selected_mention_candidates")
    if raw is None:
        raw = payload.get("mention_plan")
    if raw is None:
        return None
    return normalize_mention_plan(raw)


def require_all_mentions_flag(payload: dict[str, Any] | None, *, default: bool = True) -> bool:
    payload = payload or {}
    if "require_all_mentions" not in payload:
        return bool(default)
    val = payload.get("require_all_mentions")
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)


def evaluate_mention_plan_local(
    plan: list[dict[str, Any]],
    *,
    mentions_requested: int,
) -> dict[str, Any]:
    """Local dry-run validation (no Telegram calls)."""
    requested = max(0, int(mentions_requested or 0))
    selected = normalize_mention_plan(plan)[:requested] if requested else []
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []

    if requested > 0 and not selected:
        blockers.append("story_mention_candidate_missing")

    for cand in selected:
        username = cand.get("username")
        row = {
            **cand,
            "peer_id": cand.get("user_id"),
            "username_available": bool(username),
            "peer_id_available": cand.get("user_id") is not None,
            "peer_resolves": None,  # not checked without a Telegram session call
            "eligible_for_caption_mention": bool(username),
            "would_be_applied": bool(username),
            "blocked_by_policy": False,
            "skip_reason": None,
        }
        if not username:
            row["skip_reason"] = "story_mention_username_missing"
            row["would_be_applied"] = False
            blockers.append("story_mention_username_missing")
        rows.append(row)

    if requested > len(selected) and "story_mention_candidate_missing" not in blockers:
        blockers.append("story_mention_candidate_missing")

    return {
        "mentions_requested": requested,
        "mentions_selected": rows,
        "mention_plan_ok": requested == 0 or (len(rows) >= requested and not blockers),
        "live_blockers": sorted(set(blockers)),
        "representation": "caption_text_at_username_plus_message_entity_mention",
        "operator_summary": _operator_summary(requested, rows, blockers),
    }


def _operator_summary(requested: int, rows: list[dict[str, Any]], blockers: list[str]) -> str:
    if requested <= 0:
        return "No mentions requested."
    if blockers or not rows:
        return "Mention could not be prepared. Choose another candidate or publish without mentions."
    first = rows[0]
    uname = first.get("username")
    source = first.get("source_chat_title") or "All groups"
    return f"Selected mention: @{uname} · Source: {source} · Status: Ready"


def build_caption_with_mention_entities(
    caption: str | None,
    applied: list[dict[str, Any]],
) -> tuple[str, list[Any]]:
    """Build caption text and MessageEntityMention list for SendStoryRequest."""
    from telethon.tl.types import MessageEntityMention

    base = caption or ""
    usernames = [str(a["username"]).lstrip("@") for a in applied if a.get("username")]
    if not usernames:
        return base, []

    mention_bits = [f"@{u}" for u in usernames]
    mention_block = " ".join(mention_bits)
    if base:
        full = f"{base}\n\n{mention_block}"
        cursor = _utf16_len(base) + _utf16_len("\n\n")
    else:
        full = mention_block
        cursor = 0

    entities: list[Any] = []
    for i, bit in enumerate(mention_bits):
        entities.append(MessageEntityMention(offset=cursor, length=_utf16_len(bit)))
        cursor += _utf16_len(bit)
        if i < len(mention_bits) - 1:
            cursor += _utf16_len(" ")
    return full, entities


def empty_mention_result(*, requested: int = 0) -> dict[str, Any]:
    return {
        "mentions_requested": int(requested or 0),
        "mentions_selected": [],
        "mentions_applied": [],
        "mentions_skipped": [],
        "mention_skip_reasons": [],
        # Compatibility for older clients
        "mentions": [],
    }
