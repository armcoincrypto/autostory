"""Derived owner chat catalog for Messages (no Telegram, no execution).

Sources: scheduled DM jobs, owner DM intents, chat_targets.
Dedupes by canonical_peer_key so bare and -100 ids collapse.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.scheduler_models import ChatTarget, MessageType, ScheduledJob
from src.messaging.models import OwnerDmIntent
from src.messaging.peer_ids import canonical_peer_key, preferred_display_peer_id
from src.messaging.peer_labels import resolve_owner_peer_label


def _ts(value: Optional[datetime]) -> float:
    if value is None:
        return 0.0
    try:
        return float(value.timestamp())
    except Exception:
        return 0.0


def build_owner_chat_catalog(
    db: Session,
    *,
    limit: int = 40,
    q: Optional[str] = None,
) -> dict[str, Any]:
    """Return recent + known chats for multi-account picker (DB only)."""
    lim = max(1, min(int(limit or 40), 100))
    buckets: dict[str, dict[str, Any]] = {}

    def upsert(
        *,
        peer_id: Optional[str],
        peer_type: Optional[str],
        title: Optional[str] = None,
        username: Optional[str] = None,
        when: Optional[datetime] = None,
        source: str,
        invite_link: Optional[str] = None,
    ) -> None:
        pid = (peer_id or "").strip()
        uname = (username or "").strip().lstrip("@") or None
        if not pid and not uname:
            return
        from src.messaging.telegram_service_peers import is_sensitive_telegram_system_chat

        if is_sensitive_telegram_system_chat(
            pid or uname, peer_type=peer_type, title=title, username=uname
        ):
            return
        key = canonical_peer_key(pid or (f"@{uname}" if uname else ""), peer_type)
        if not key:
            return
        display_peer = preferred_display_peer_id(pid, peer_type) if pid else (f"@{uname}" if uname else "")
        label = None
        if title and str(title).strip():
            label = str(title).strip()
        elif uname:
            label = f"@{uname}"
        else:
            label = resolve_owner_peer_label(db, display_peer or pid, peer_type, peer_username=uname)

        # Never prefer raw numeric as primary label when we have a better one later.
        score = _ts(when)
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = {
                "key": key,
                "peer_id": display_peer or pid,
                "peer_type": (peer_type or "").strip().lower() or None,
                "title": label,
                "username": uname,
                "invite_link": invite_link,
                "last_used_at": when.isoformat() + "Z" if when else None,
                "_score": score,
                "sources": [source],
                "ref": invite_link or (f"@{uname}" if uname else display_peer or pid),
            }
            return
        existing["sources"] = sorted(set(existing.get("sources") or []) | {source})
        if score >= float(existing.get("_score") or 0):
            existing["_score"] = score
            existing["last_used_at"] = when.isoformat() + "Z" if when else existing.get("last_used_at")
        if peer_type and not existing.get("peer_type"):
            existing["peer_type"] = peer_type
        if display_peer and (
            not existing.get("peer_id")
            or (str(existing.get("peer_id") or "").isdigit() and display_peer.startswith("-"))
        ):
            existing["peer_id"] = display_peer
            existing["ref"] = invite_link or existing.get("ref") or display_peer
        if invite_link and not existing.get("invite_link"):
            existing["invite_link"] = invite_link
            existing["ref"] = invite_link
        if uname and not existing.get("username"):
            existing["username"] = uname
        # Upgrade label away from generic / numeric-looking
        cur = str(existing.get("title") or "")
        if label and (
            not cur
            or cur in {"Chat", "Group chat", "Channel", "Private chat", "Bot"}
            or cur.lstrip("-").isdigit()
            or (cur.startswith("-100") and cur[4:].isdigit())
        ):
            existing["title"] = label

    # Scheduled DM peers
    for job in (
        db.query(ScheduledJob)
        .filter(ScheduledJob.type == MessageType.DM.value)
        .filter(ScheduledJob.peer_id.isnot(None))
        .order_by(ScheduledJob.run_at.desc(), ScheduledJob.id.desc())
        .limit(200)
        .all()
    ):
        upsert(
            peer_id=job.peer_id,
            peer_type=job.peer_type,
            when=job.run_at or job.updated_at or job.created_at,
            source="scheduled",
        )

    # Recent owner DM intents
    for intent in (
        db.query(OwnerDmIntent)
        .order_by(OwnerDmIntent.id.desc())
        .limit(200)
        .all()
    ):
        upsert(
            peer_id=intent.peer_id,
            peer_type=intent.peer_type,
            username=intent.peer_username,
            when=intent.updated_at or intent.created_at,
            source="messages",
        )

    # Known chat_targets (joined/resolved scheduler targets)
    for target in (
        db.query(ChatTarget)
        .order_by(ChatTarget.updated_at.desc(), ChatTarget.id.desc())
        .limit(200)
        .all()
    ):
        pt = (target.chat_type or "supergroup").strip().lower()
        peer = None
        if target.tg_id is not None:
            peer = preferred_display_peer_id(str(target.tg_id), pt)
        upsert(
            peer_id=peer,
            peer_type=pt,
            title=target.title,
            username=target.username,
            when=target.updated_at or target.created_at,
            source="known",
            invite_link=(target.invite_link or None),
        )

    rows = list(buckets.values())
    rows.sort(key=lambda r: float(r.get("_score") or 0), reverse=True)

    query = (q or "").strip().lower()
    if query:
        filtered = []
        for r in rows:
            hay = " ".join(
                [
                    str(r.get("title") or ""),
                    str(r.get("username") or ""),
                    str(r.get("peer_id") or ""),
                    str(r.get("invite_link") or ""),
                    str(r.get("ref") or ""),
                ]
            ).lower()
            if query in hay or query.lstrip("@") in hay:
                filtered.append(r)
        rows = filtered

    recent = []
    known = []
    for r in rows:
        item = {k: v for k, v in r.items() if not k.startswith("_")}
        # Hide raw-id-looking primary titles under generic fallbacks already handled
        srcs = set(item.get("sources") or [])
        if "scheduled" in srcs or "messages" in srcs:
            if len(recent) < lim:
                recent.append(item)
        if "known" in srcs or True:
            if len(known) < lim:
                known.append(item)

    # Known list: prefer chat_targets + anything not already shown; still capped
    known_only = []
    seen = set()
    for r in rows:
        item = {k: v for k, v in r.items() if not k.startswith("_")}
        key = item.get("key")
        if key in seen:
            continue
        seen.add(key)
        known_only.append(item)
        if len(known_only) >= lim:
            break

    return {
        "ok": True,
        "recent": recent[: min(15, lim)],
        "known": known_only,
        "count": len(buckets),
    }
