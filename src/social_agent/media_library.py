"""Media library — local uploads, folders, hash dedup, safe soft-delete.

No provider upload. CDN abstracted behind storage_path / future URL helper.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from src.social_agent.models import SocialMediaAsset, SocialMediaFolder, SocialMediaUsage

ALLOWED_IMAGE = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
ALLOWED_VIDEO = frozenset({"video/mp4", "video/webm", "video/quicktime"})
MAX_BYTES = 50 * 1024 * 1024  # 50 MiB


def media_root() -> Path:
    raw = (os.environ.get("SOCIAL_MEDIA_LIBRARY_ROOT") or "").strip()
    if raw:
        root = Path(raw)
    else:
        # Prefer shared production data dir when present.
        for candidate in (Path("/opt/autostory/data/social_media"), Path("data/social_media")):
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                root = candidate
                break
            except OSError:
                continue
        else:
            root = Path("data/social_media")
            root.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(name: str) -> str:
    base = Path(name or "upload.bin").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "upload.bin"
    return cleaned[:180]


def _kind_for_mime(mime: str) -> str:
    if mime in ALLOWED_IMAGE:
        return "image"
    if mime in ALLOWED_VIDEO:
        return "video"
    return "other"


def _asset_dict(row: SocialMediaAsset) -> dict[str, Any]:
    return {
        "id": row.id,
        "folder_id": row.folder_id,
        "filename": row.filename,
        "original_filename": row.original_filename,
        "mime_type": row.mime_type,
        "kind": row.kind,
        "byte_size": row.byte_size,
        "content_hash": row.content_hash,
        "width": row.width,
        "height": row.height,
        "duration_ms": row.duration_ms,
        "usage_count": row.usage_count,
        "metadata": json.loads(row.metadata_json or "{}"),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "deleted": row.deleted_at is not None,
        # Relative path only — never expose absolute host secrets.
        "storage_key": row.storage_path,
        "thumbnail_key": row.thumbnail_path,
        "cdn": {"provider": "local", "status": "local_only"},
    }


def list_folders(db: Session, *, workspace_id: str = "default") -> dict[str, Any]:
    rows = (
        db.query(SocialMediaFolder)
        .filter(SocialMediaFolder.workspace_id == workspace_id)
        .order_by(SocialMediaFolder.name.asc())
        .all()
    )
    return {
        "ok": True,
        "folders": [
            {"id": r.id, "name": r.name, "parent_id": r.parent_id, "created_by": r.created_by}
            for r in rows
        ],
    }


def create_folder(
    db: Session,
    *,
    actor: str | None,
    name: str,
    parent_id: int | None = None,
    workspace_id: str = "default",
) -> dict[str, Any]:
    label = (name or "").strip()[:255]
    if not label:
        return {"ok": False, "error": "name_required"}
    row = SocialMediaFolder(
        workspace_id=workspace_id,
        name=label,
        parent_id=int(parent_id) if parent_id is not None else None,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    return {"ok": True, "folder": {"id": row.id, "name": row.name, "parent_id": row.parent_id}}


def list_assets(
    db: Session,
    *,
    workspace_id: str = "default",
    folder_id: int | None = None,
    include_deleted: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    q = db.query(SocialMediaAsset).filter(SocialMediaAsset.workspace_id == workspace_id)
    if not include_deleted:
        q = q.filter(SocialMediaAsset.deleted_at.is_(None))
    if folder_id is not None:
        q = q.filter(SocialMediaAsset.folder_id == int(folder_id))
    rows = q.order_by(SocialMediaAsset.id.desc()).limit(max(1, min(limit, 500))).all()
    return {"ok": True, "assets": [_asset_dict(r) for r in rows]}


def upload_asset(
    db: Session,
    *,
    actor: str | None,
    filename: str,
    stream: BinaryIO,
    mime_type: str | None = None,
    folder_id: int | None = None,
    workspace_id: str = "default",
) -> dict[str, Any]:
    data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return {"ok": False, "error": "file_too_large", "max_bytes": MAX_BYTES}
    if not data:
        return {"ok": False, "error": "empty_file"}
    mime = (mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").split(";")[0].strip().lower()
    if mime not in ALLOWED_IMAGE | ALLOWED_VIDEO:
        return {"ok": False, "error": "unsupported_mime", "mime_type": mime}
    digest = hashlib.sha256(data).hexdigest()
    existing = (
        db.query(SocialMediaAsset)
        .filter(
            SocialMediaAsset.workspace_id == workspace_id,
            SocialMediaAsset.content_hash == digest,
            SocialMediaAsset.deleted_at.is_(None),
        )
        .first()
    )
    if existing:
        return {
            "ok": True,
            "deduplicated": True,
            "asset": _asset_dict(existing),
            "message": "Identical content already in library (hash match).",
        }

    safe = _safe_name(filename)
    rel = f"{workspace_id}/{digest[:2]}/{digest}_{safe}"
    dest = media_root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    # Thumbnail: for images reuse original path; videos leave null until ffmpeg pipeline.
    thumb_rel = rel if mime in ALLOWED_IMAGE else None

    row = SocialMediaAsset(
        workspace_id=workspace_id,
        folder_id=int(folder_id) if folder_id is not None else None,
        filename=safe,
        original_filename=_safe_name(filename),
        mime_type=mime,
        kind=_kind_for_mime(mime),
        byte_size=len(data),
        content_hash=digest,
        storage_path=rel,
        thumbnail_path=thumb_rel,
        metadata_json=json.dumps({"upload_id": uuid.uuid4().hex}),
        created_by=actor,
    )
    db.add(row)
    db.flush()
    return {"ok": True, "deduplicated": False, "asset": _asset_dict(row)}


def soft_delete_asset(
    db: Session,
    *,
    actor: str | None,
    asset_id: int,
    workspace_id: str = "default",
) -> dict[str, Any]:
    row = (
        db.query(SocialMediaAsset)
        .filter(SocialMediaAsset.id == int(asset_id), SocialMediaAsset.workspace_id == workspace_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not_found"}
    if row.deleted_at is not None:
        return {"ok": True, "already_deleted": True, "asset_id": row.id}
    if row.usage_count > 0:
        return {
            "ok": False,
            "error": "asset_in_use",
            "usage_count": row.usage_count,
            "message": "Detach from content before deleting.",
        }
    row.deleted_at = datetime.utcnow()
    db.flush()
    return {"ok": True, "deleted": True, "asset_id": row.id, "actor": actor}


def record_usage(
    db: Session,
    *,
    actor: str | None,
    asset_id: int,
    content_id: int | None = None,
    context: str = "attached",
) -> dict[str, Any]:
    row = db.query(SocialMediaAsset).filter(SocialMediaAsset.id == int(asset_id)).first()
    if not row or row.deleted_at is not None:
        return {"ok": False, "error": "not_found"}
    db.add(
        SocialMediaUsage(
            asset_id=row.id,
            content_id=int(content_id) if content_id is not None else None,
            context=(context or "attached")[:128],
            actor=actor,
        )
    )
    row.usage_count = int(row.usage_count or 0) + 1
    db.flush()
    return {"ok": True, "asset_id": row.id, "usage_count": row.usage_count}


def usage_history(db: Session, asset_id: int) -> dict[str, Any]:
    rows = (
        db.query(SocialMediaUsage)
        .filter(SocialMediaUsage.asset_id == int(asset_id))
        .order_by(SocialMediaUsage.id.desc())
        .limit(100)
        .all()
    )
    return {
        "ok": True,
        "usages": [
            {
                "id": r.id,
                "content_id": r.content_id,
                "context": r.context,
                "actor": r.actor,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
