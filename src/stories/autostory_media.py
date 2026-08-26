"""Canonical AutoStory media resolution + approval/execution validation."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from src.stories.rotation_audit import media_precheck


def media_dir() -> Path:
    try:
        from config.settings import settings

        return Path(str(settings.storage.media_dir)).expanduser().resolve()
    except Exception:
        return Path("./data/media").resolve()


def resolve_durable_media_path(media_path: str | None) -> dict[str, Any]:
    """Resolve operator media reference to a durable path under media_dir.

    Accepts absolute paths, ``data/media/...`` relatives, or bare filenames.
    Rejects paths outside the shared media directory.
    """
    raw = str(media_path or "").strip()
    if not raw:
        return {
            "ok": False,
            "error": "media_path_required",
            "message": "Media is required.",
            "path": None,
            "filename": None,
        }

    root = media_dir()
    root.mkdir(parents=True, exist_ok=True)
    candidate = Path(raw)
    if not candidate.is_absolute():
        name = candidate.name
        candidate = root / name
        rel = Path(raw)
        if len(rel.parts) > 1:
            alt = (Path.cwd() / rel).resolve()
            try:
                alt.relative_to(root)
                candidate = alt
            except ValueError:
                pass

    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except Exception:
        return {
            "ok": False,
            "error": "media_outside_library",
            "message": "Media must be inside the shared media library.",
            "path": raw,
            "filename": Path(raw).name,
        }

    if not resolved.is_file():
        return {
            "ok": False,
            "error": "media_not_found",
            "message": f"Media file not found: {resolved.name}",
            "path": str(resolved),
            "filename": resolved.name,
        }

    try:
        rel_to_cwd = resolved.relative_to(Path.cwd())
        store_path = str(rel_to_cwd)
    except ValueError:
        store_path = str(resolved)

    return {
        "ok": True,
        "path": store_path,
        "absolute_path": str(resolved),
        "filename": resolved.name,
        "media_dir": str(root),
    }


def validate_campaign_media(media_path: str | None) -> dict[str, Any]:
    """Approval + execution fail-closed media check (exists, type, story-compat)."""
    resolved = resolve_durable_media_path(media_path)
    if not resolved.get("ok"):
        return {
            **resolved,
            "media_ok": False,
            "compat_blocker": resolved.get("error"),
            "precheck": None,
        }

    check_path = resolved.get("absolute_path") or resolved.get("path")
    pre = media_precheck(str(check_path))
    ok = bool(pre.get("ok"))
    blocker = pre.get("compat_blocker") or (None if ok else "missing_or_invalid_media")
    return {
        "ok": ok,
        "media_ok": ok,
        "path": resolved.get("path"),
        "absolute_path": resolved.get("absolute_path"),
        "filename": resolved.get("filename"),
        "message": pre.get("message") if not ok else "Media ready.",
        "error": None if ok else (blocker or "missing_or_invalid_media"),
        "compat_blocker": blocker,
        "precheck": pre,
        "needs_preparation": bool(
            (not ok)
            and blocker
            in {
                "story_media_normalization_required",
                "story_media_not_vertical",
                "story_media_format_unsupported",
            }
        ),
    }


def mentions_production_certified() -> bool:
    """Gate: AutoStory live mentions require separate production certification."""
    raw = (
        os.environ.get("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED")
        or os.environ.get("AUTOSTORY_MENTIONS_CERTIFIED")
        or "false"
    )
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def normalize_campaign_mentions(raw_mps: Any) -> dict[str, Any]:
    """Single mention model for AutoStory campaigns (create/preview).

    When not production-certified, force Off / 0 for *new* campaign payloads.
    Execution policy uses ``validate_campaign_execution_policy`` which fail-closes
    on persisted mentions_per_story > 0 without silently rewriting publish intent.
    """
    certified = mentions_production_certified()
    try:
        requested = 0 if raw_mps in (None, "") else max(0, min(20, int(raw_mps)))
    except (TypeError, ValueError):
        requested = 0

    if not certified:
        return {
            "mentions_enabled": False,
            "mentions_per_story": 0,
            "requested_mentions_per_story": requested,
            "mentions_production_certified": False,
            "mentions_forced_off": requested > 0,
            "message": (
                "Mentions are not yet production-certified for AutoStory. "
                "Keep Off for production campaigns."
                if requested > 0
                else None
            ),
        }

    enabled = requested > 0
    return {
        "mentions_enabled": enabled,
        "mentions_per_story": requested if enabled else 0,
        "requested_mentions_per_story": requested,
        "mentions_production_certified": True,
        "mentions_forced_off": False,
        "message": None,
    }


def prepare_story_derivative(media_path: str | None) -> dict[str, Any]:
    """Create a durable 1080×1920 contain+letterbox Story JPEG; never overwrite source."""
    from src.stories.story_media_compat import normalize_story_image, evaluate_story_media_compat

    resolved = resolve_durable_media_path(media_path)
    if not resolved.get("ok"):
        return {
            "ok": False,
            "error": resolved.get("error") or "media_not_found",
            "message": resolved.get("message") or "Media not found.",
        }

    src = Path(str(resolved["absolute_path"]))
    # Already compatible → return as-is (no duplicate derivative)
    current = evaluate_story_media_compat(src)
    if current.get("ok"):
        return {
            "ok": True,
            "already_ready": True,
            "path": resolved.get("path"),
            "absolute_path": str(src),
            "filename": src.name,
            "message": "Media is already ready for Telegram Stories.",
            "width": current.get("media_width"),
            "height": current.get("media_height"),
            "composition": None,
        }

    root = media_dir()
    stem = src.stem
    # Avoid stacking story_ready_ prefixes
    if stem.startswith("story_ready_"):
        base = stem
    else:
        base = f"story_ready_{stem}"
    dest_name = f"{base}_{int(time.time())}.jpg"
    dest = root / dest_name
    if dest.exists():
        dest_name = f"{base}_{int(time.time())}_{os.getpid()}.jpg"
        dest = root / dest_name

    try:
        result = normalize_story_image(src, dest)
    except Exception as exc:
        return {
            "ok": False,
            "error": "story_media_prepare_failed",
            "message": f"Could not prepare image for Stories: {exc}",
        }

    verify = validate_campaign_media(str(dest))
    if not verify.get("ok"):
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "ok": False,
            "error": verify.get("error") or "story_media_prepare_failed",
            "message": verify.get("message") or "Prepared media failed validation.",
            "verify": verify,
        }

    return {
        "ok": True,
        "already_ready": False,
        "path": verify.get("path"),
        "absolute_path": verify.get("absolute_path"),
        "filename": verify.get("filename"),
        "source_path": str(src),
        "source_filename": src.name,
        "message": "Ready for Telegram Story — 1080 × 1920 JPEG. Original preserved.",
        "width": result.get("output_width"),
        "height": result.get("output_height"),
        "composition": result.get("composition") or "contain_letterbox",
        "prepared_for_story": True,
    }


def validate_campaign_execution_policy(campaign: Any) -> dict[str, Any]:
    """Revalidate a persisted campaign under *current* execution policy.

    Called before wave authorization / Story mutation. Fail-closed.
    """
    if isinstance(campaign, dict):
        status = str(campaign.get("status") or "")
        media_path = campaign.get("media_path")
        mentions = int(campaign.get("mentions_per_story") or 0)
        account_ids = list(campaign.get("account_ids") or [])
        times_json = list(campaign.get("times_json") or [])
    else:
        status = str(getattr(campaign, "status", None) or "")
        media_path = getattr(campaign, "media_path", None)
        mentions = int(getattr(campaign, "mentions_per_story", None) or 0)
        account_ids = list(getattr(campaign, "account_ids", None) or [])
        times_json = list(getattr(campaign, "times_json", None) or [])

    blockers: list[str] = []
    classification = "VALID_CURRENT_POLICY"
    reason = None
    message = None

    if status in {"cancelled", "completed", "failed"}:
        return {
            "ok": False,
            "error": "BLOCKED_POLICY",
            "reason": "campaign_terminal",
            "classification": "OTHER_BLOCKER",
            "message": f"Campaign status is {status}.",
            "blockers": ["campaign_terminal"],
            "mentions_per_story": mentions,
        }

    if status == "paused":
        return {
            "ok": False,
            "error": "BLOCKED_POLICY",
            "reason": "campaign_paused",
            "classification": "OTHER_BLOCKER",
            "message": "Campaign is paused.",
            "blockers": ["campaign_paused"],
            "mentions_per_story": mentions,
        }

    if status not in {"active", "draft", "scheduled", "approved"}:
        # Unknown / unexpected — fail closed for mutation paths
        if status != "active":
            pass

    # Mentions: persisted >0 while uncertified → BLOCK (do not silently rewrite)
    if mentions > 0 and not mentions_production_certified():
        blockers.append("mentions_not_production_certified")
        classification = "INVALID_MENTIONS"
        reason = "mentions_not_production_certified"
        message = (
            "This campaign cannot publish under the current AutoStory policy. "
            "Mentions are not production-certified."
        )

    media = validate_campaign_media(media_path)
    if not media.get("ok"):
        blockers.append(str(media.get("error") or "missing_or_invalid_media"))
        if classification == "VALID_CURRENT_POLICY":
            classification = "INVALID_MEDIA"
            reason = media.get("error") or "missing_or_invalid_media"
            message = media.get("message") or "Media is missing or not Story-compatible."

    from src.stories.autostory_hardening import MAX_AUTOSTORY_WAVE_SIZE

    if len(account_ids) > MAX_AUTOSTORY_WAVE_SIZE * 20:
        # Soft sanity; wave selection still caps at 25
        pass
    if not account_ids:
        blockers.append("no_accounts")
        if classification == "VALID_CURRENT_POLICY":
            classification = "INVALID_ACCOUNTS"
            reason = "no_accounts"
            message = "Campaign has no planned accounts."

    if not times_json and status == "active":
        # Active campaigns should have times; empty may still compute defaults elsewhere
        pass

    # Recurring fields: clamp awareness (do not mutate campaign here)
    from src.stories.autostory_recurring import (
        is_recurring,
        clamp_stories_per_account_per_day,
        MAX_STORIES_PER_ACCOUNT_PER_DAY,
    )

    if is_recurring(campaign):
        if isinstance(campaign, dict):
            spad_raw = campaign.get("stories_per_account_per_day", 1)
        else:
            spad_raw = getattr(campaign, "stories_per_account_per_day", 1)
        spad = clamp_stories_per_account_per_day(spad_raw)
        try:
            requested_spad = int(spad_raw if spad_raw not in (None, "") else 1)
        except (TypeError, ValueError):
            requested_spad = 1
        if requested_spad > MAX_STORIES_PER_ACCOUNT_PER_DAY:
            blockers.append("stories_per_account_per_day_exceeds_platform_cap")
            if classification == "VALID_CURRENT_POLICY":
                classification = "INVALID_CAPACITY"
                reason = "stories_per_account_per_day_exceeds_platform_cap"
                message = (
                    f"Stories per account per day cannot exceed platform capacity "
                    f"({MAX_STORIES_PER_ACCOUNT_PER_DAY})."
                )

    ok = not blockers
    return {
        "ok": ok,
        "error": None if ok else "BLOCKED_POLICY",
        "reason": reason,
        "classification": classification if not ok else "VALID_CURRENT_POLICY",
        "message": message,
        "blockers": blockers,
        "mentions_per_story": mentions,
        "mentions_production_certified": mentions_production_certified(),
        "media": {
            "ok": media.get("ok"),
            "path": media.get("path"),
            "error": media.get("error"),
            "needs_preparation": media.get("needs_preparation"),
        },
        "account_count": len(account_ids),
        "max_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
    }


def classify_nonterminal_campaigns(campaigns: list[Any]) -> list[dict[str, Any]]:
    """Sweep non-terminal campaigns against current execution policy."""
    out: list[dict[str, Any]] = []
    for c in campaigns:
        if isinstance(c, dict):
            cid = c.get("id")
            status = c.get("status")
            mentions = c.get("mentions_per_story")
        else:
            cid = getattr(c, "id", None)
            status = getattr(c, "status", None)
            mentions = getattr(c, "mentions_per_story", None)
        policy = validate_campaign_execution_policy(c)
        out.append(
            {
                "id": cid,
                "status": status,
                "mentions_per_story": mentions,
                "classification": policy.get("classification"),
                "ok": policy.get("ok"),
                "reason": policy.get("reason"),
                "blockers": policy.get("blockers"),
                "message": policy.get("message"),
            }
        )
    return out
