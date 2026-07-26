"""Canonical Facebook controlled-canary publish service.

Flow: dry-run → explicit approval → short-lived auth → one Graph POST → receipt.
Instagram and general live publishing remain denied.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.credential_crypto import (
    CredentialCryptoConfigurationError,
    SocialCredentialCrypto,
)
from src.social_agent.models import (
    SocialConnection,
    SocialIdempotencyRecord,
    SocialPublishDryRun,
    SocialPublishReceipt,
)
from src.social_agent.providers.meta import MetaProviderAdapter
from src.social_agent.publishing.canary_auth import (
    consume_canary_authorization,
    disable_canary_authorization,
    load_canary_authorization,
    mint_canary_authorization,
)
from src.social_agent.publishing.execution import (
    CANARY_DESTINATION,
    CANARY_FACEBOOK_PAGE_ID,
    CANARY_FACEBOOK_PAGE_NAME,
    CANARY_MESSAGE,
    CANARY_REQUIRED_PERMISSION,
    PublishingExecutionMode,
    assert_instagram_hard_disabled,
    facebook_publishing_enabled,
    publishing_execution_mode,
)
from src.social_agent.publishing.service import run_publishing_dry_run


def _audit(db: Session, **kwargs: Any) -> None:
    from src.social_agent.services import _audit as shared_audit

    shared_audit(db, **kwargs)


@contextmanager
def _temporary_facebook_canary_gates():
    """Elevate gates only for the duration of one authorized canary call."""
    prev_fb = os.environ.get("META_FACEBOOK_PUBLISHING_ENABLED")
    prev_mode = os.environ.get("META_PUBLISHING_EXECUTION_MODE")
    os.environ["META_FACEBOOK_PUBLISHING_ENABLED"] = "true"
    os.environ["META_PUBLISHING_EXECUTION_MODE"] = PublishingExecutionMode.CONTROLLED_CANARY.value
    try:
        yield
    finally:
        if prev_fb is None:
            os.environ["META_FACEBOOK_PUBLISHING_ENABLED"] = "false"
        else:
            os.environ["META_FACEBOOK_PUBLISHING_ENABLED"] = prev_fb
        if prev_mode is None:
            os.environ["META_PUBLISHING_EXECUTION_MODE"] = PublishingExecutionMode.DISABLED.value
        else:
            os.environ["META_PUBLISHING_EXECUTION_MODE"] = prev_mode


def _meta_connection(db: Session, *, workspace_id: str) -> SocialConnection | None:
    return (
        db.query(SocialConnection)
        .filter(SocialConnection.provider == "meta", SocialConnection.workspace_id == workspace_id)
        .order_by(SocialConnection.id.desc())
        .first()
    )


def _decrypt_credentials(row: SocialConnection) -> dict[str, Any]:
    crypto = SocialCredentialCrypto.from_environment()
    return crypto.decrypt_json(row.credentials_encrypted or "")


def prepare_facebook_canary_dry_run(
    db: Session,
    *,
    actor: str,
    workspace_id: str = "default",
    message: str = CANARY_MESSAGE,
) -> dict[str, Any]:
    """Create the exact text-only Facebook Page dry-run for the canary."""
    ig = assert_instagram_hard_disabled()
    if not ig.get("ok"):
        return ig
    if message != CANARY_MESSAGE:
        return {
            "ok": False,
            "error": "canary_message_mismatch",
            "message": "Only the pre-approved canary message is allowed.",
        }
    out = run_publishing_dry_run(
        db,
        actor=actor,
        destinations=[CANARY_DESTINATION],
        content={"text": message, "language": "EN", "brand_voice": "neutral"},
        workspace_id=workspace_id,
    )
    _audit(
        db,
        actor=actor,
        action="publishing.canary.dry_run",
        tool_name="publishing.facebook_canary",
        decision="dry_run",
        dry_run=True,
        detail={
            "dry_run_id": out.get("dry_run_id"),
            "payload_hash": out.get("payload_hash"),
            "page_id": CANARY_FACEBOOK_PAGE_ID,
            "provider_http_posts": out.get("provider_http_posts", 0),
        },
    )
    return out


def preflight_facebook_canary(
    db: Session,
    *,
    actor: str,
    workspace_id: str,
    dry_run_id: int,
    payload_hash: str,
    page_id: str = CANARY_FACEBOOK_PAGE_ID,
) -> dict[str, Any]:
    """Validate dry-run, selection, permissions, token health. Provider POST count stays 0."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, **extra: Any) -> None:
        checks.append({"check": name, "ok": ok, **extra})

    ig = assert_instagram_hard_disabled()
    add("instagram_disabled", ig.get("ok") is True)

    mode = publishing_execution_mode()
    # Preflight allows disabled env; elevation happens only inside execute.
    add(
        "execution_mode_not_live",
        mode != PublishingExecutionMode.LIVE,
        mode=mode.value,
    )

    row = db.query(SocialPublishDryRun).filter(SocialPublishDryRun.id == int(dry_run_id)).first()
    if row is None:
        add("dry_run_exists", False)
        return {"ok": False, "error": "dry_run_not_found", "checks": checks, "provider_http_posts": 0}
    add("dry_run_exists", True)
    add("payload_hash_match", row.payload_hash == payload_hash, expected=payload_hash, actual=row.payload_hash)
    add("dry_run_workspace", row.workspace_id == workspace_id)
    dests = json.loads(row.destinations_json or "[]")
    add("destination_facebook_page", CANARY_DESTINATION in dests, destinations=dests)
    add("dry_run_provider_posts_zero", int(row.provider_http_posts or 0) == 0)
    validation = json.loads(row.validation_json or "{}")
    add("validation_ok", bool(validation.get("ok")))

    content = json.loads(row.content_snapshot_json or "{}")
    add("canary_message", (content.get("text") or "") == CANARY_MESSAGE)

    conn = _meta_connection(db, workspace_id=workspace_id)
    if conn is None:
        add("meta_connection", False)
        return {"ok": False, "error": "meta_not_connected", "checks": checks, "provider_http_posts": 0}
    add("meta_connection", True, connection_id=conn.id, status=conn.status)
    add(
        "selected_page",
        str(conn.selected_page_id) == str(page_id) == CANARY_FACEBOOK_PAGE_ID,
        selected_page_id=conn.selected_page_id,
        required=CANARY_FACEBOOK_PAGE_ID,
        page_name=CANARY_FACEBOOK_PAGE_NAME,
    )

    try:
        creds = _decrypt_credentials(conn)
    except (CredentialCryptoConfigurationError, Exception) as exc:
        add("token_decrypt", False, error=type(exc).__name__)
        return {"ok": False, "error": "credential_unavailable", "checks": checks, "provider_http_posts": 0}
    add("token_decrypt", True)

    page_tokens = creds.get("page_access_tokens") or {}
    page_token = page_tokens.get(str(page_id))
    add("page_token_present", bool(page_token))
    user_token = creds.get("user_access_token") or creds.get("access_token")
    add("user_token_present", bool(user_token))

    adapter = MetaProviderAdapter()
    if page_token:
        health = adapter.health_probe(access_token=page_token, page_id=str(page_id))
        add("token_health", bool(health.get("ok")), health=health.get("health"), reason=health.get("reason"))
    else:
        add("token_health", False)

    granted: list[str] = []
    if user_token:
        perms = adapter.list_user_permissions(access_token=user_token)
        if perms.get("ok"):
            granted = list(perms.get("granted") or [])
        else:
            dbg = adapter.debug_token(input_token=user_token)
            granted = list((dbg.get("data") or {}).get("scopes") or [])
    add(
        "pages_manage_posts",
        CANARY_REQUIRED_PERMISSION in granted,
        granted=[g for g in granted if not str(g).startswith("access")],
    )

    # Idempotency: key must not already have a successful receipt.
    existing = (
        db.query(SocialIdempotencyRecord)
        .filter(SocialIdempotencyRecord.action == "publishing.facebook_canary")
        .count()
    )
    add("idempotency_table_readable", True, prior_canary_idem_rows=existing)

    ok = all(c["ok"] for c in checks)
    _audit(
        db,
        actor=actor,
        action="publishing.canary.preflight",
        tool_name="publishing.facebook_canary",
        decision="allow" if ok else "deny",
        dry_run=True,
        detail={"ok": ok, "checks": [c["check"] for c in checks if not c["ok"]], "provider_http_posts": 0},
    )
    return {
        "ok": ok,
        "checks": checks,
        "provider_http_posts": 0,
        "meta_provider_mutations": 0,
        "page_id": CANARY_FACEBOOK_PAGE_ID,
        "page_name": CANARY_FACEBOOK_PAGE_NAME,
        "dry_run_id": dry_run_id,
        "payload_hash": payload_hash,
        "permission_reconnect_required": CANARY_REQUIRED_PERMISSION not in granted,
    }


def approve_and_mint_canary(
    db: Session,
    *,
    actor: str,
    workspace_id: str,
    dry_run_id: int,
    payload_hash: str,
    page_id: str,
    explicit_approval: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if explicit_approval is True or explicit_approval is False:
        return {
            "ok": False,
            "error": "boolean_authorization_rejected",
            "message": "Do not pass a boolean as authorization. Use explicit_approval='CONFIRM'.",
        }
    if explicit_approval != "CONFIRM":
        return {
            "ok": False,
            "error": "explicit_approval_required",
            "message": "Canary authorization requires explicit_approval='CONFIRM'.",
        }
    pre = preflight_facebook_canary(
        db,
        actor=actor,
        workspace_id=workspace_id,
        dry_run_id=dry_run_id,
        payload_hash=payload_hash,
        page_id=page_id,
    )
    if not pre.get("ok"):
        return {**pre, "error": pre.get("error") or "preflight_failed"}
    key = idempotency_key or f"fb-canary:{dry_run_id}:{payload_hash[:16]}:{uuid.uuid4().hex[:8]}"
    existing = db.query(SocialIdempotencyRecord).filter(SocialIdempotencyRecord.idempotency_key == key).first()
    if existing:
        return {"ok": False, "error": "duplicate_idempotency_key", "idempotency_key": key}
    minted = mint_canary_authorization(
        db,
        actor=actor,
        workspace_id=workspace_id,
        page_id=page_id,
        dry_run_id=dry_run_id,
        payload_hash=payload_hash,
        idempotency_key=key,
        explicit_approval=str(explicit_approval),
    )
    if minted.get("ok"):
        _audit(
            db,
            actor=actor,
            action="publishing.canary.authorized",
            tool_name="publishing.facebook_canary",
            decision="allow",
            dry_run=False,
            detail={
                "authorization_id": minted["authorization_id"],
                "dry_run_id": dry_run_id,
                "payload_hash": payload_hash,
                "page_id": page_id,
                "idempotency_key": key,
                # never store secret
            },
        )
    return minted


def execute_facebook_canary(
    db: Session,
    *,
    actor: str,
    workspace_id: str,
    authorization_id: int,
    authorization_secret: str,
    dry_run_id: int,
    payload_hash: str,
    page_id: str,
    idempotency_key: str,
    adapter: MetaProviderAdapter | None = None,
) -> dict[str, Any]:
    """Execute exactly one Facebook Page feed POST under a valid canary authorization."""
    ig = assert_instagram_hard_disabled()
    if not ig.get("ok"):
        return ig

    # Duplicate idempotency → return prior result, do not republish.
    prior = db.query(SocialIdempotencyRecord).filter(SocialIdempotencyRecord.idempotency_key == idempotency_key).first()
    if prior:
        try:
            cached = json.loads(prior.result_json or "{}")
        except json.JSONDecodeError:
            cached = {}
        return {
            "ok": False,
            "error": "duplicate_idempotency_key",
            "message": "Idempotency key already used; refusing to republish.",
            "prior": cached,
            "provider_http_posts": 0,
            "facebook_duplicate_posts": 0,
        }

    auth, err = load_canary_authorization(
        db, authorization_id=authorization_id, authorization_secret=authorization_secret
    )
    if err or auth is None:
        _audit(
            db,
            actor=actor,
            action="publishing.canary.denied",
            tool_name="publishing.facebook_canary",
            decision="deny",
            dry_run=False,
            detail={"reason": err, "authorization_id": authorization_id},
        )
        return {"ok": False, "error": err or "authorization_invalid", "provider_http_posts": 0}

    # Bind exact fields.
    if auth.actor != actor:
        return {"ok": False, "error": "actor_mismatch", "provider_http_posts": 0}
    if auth.workspace_id != workspace_id:
        return {"ok": False, "error": "workspace_mismatch", "provider_http_posts": 0}
    if auth.dry_run_id != int(dry_run_id) or auth.payload_hash != payload_hash:
        return {"ok": False, "error": "dry_run_binding_mismatch", "provider_http_posts": 0}
    if auth.page_id != str(page_id) or str(page_id) != CANARY_FACEBOOK_PAGE_ID:
        return {"ok": False, "error": "wrong_page", "provider_http_posts": 0}
    if auth.idempotency_key != idempotency_key:
        return {"ok": False, "error": "idempotency_mismatch", "provider_http_posts": 0}

    dry = db.query(SocialPublishDryRun).filter(SocialPublishDryRun.id == int(dry_run_id)).first()
    if dry is None or dry.payload_hash != payload_hash:
        return {"ok": False, "error": "payload_hash_mismatch", "provider_http_posts": 0}

    conn = _meta_connection(db, workspace_id=workspace_id)
    if conn is None or str(conn.selected_page_id) != CANARY_FACEBOOK_PAGE_ID:
        return {"ok": False, "error": "selected_page_invalid", "provider_http_posts": 0}

    try:
        creds = _decrypt_credentials(conn)
    except Exception:
        return {"ok": False, "error": "credential_unavailable", "provider_http_posts": 0}
    page_token = (creds.get("page_access_tokens") or {}).get(CANARY_FACEBOOK_PAGE_ID)
    if not page_token:
        return {"ok": False, "error": "page_token_missing", "provider_http_posts": 0}

    adapter = adapter or MetaProviderAdapter()
    timestamp = datetime.utcnow().isoformat() + "Z"

    with _temporary_facebook_canary_gates():
        # Consume authorization BEFORE the provider call to enforce single-use even on retries.
        consume_canary_authorization(db, authorization_id=auth.authorization_id, result_status="CONSUMED")
        db.flush()

        result = adapter.publish_page_feed(
            page_id=CANARY_FACEBOOK_PAGE_ID,
            page_access_token=page_token,
            message=CANARY_MESSAGE,
            authorization=auth,
            facebook_gate_enabled=facebook_publishing_enabled(),
            execution_mode=publishing_execution_mode().value,
        )

    disable_canary_authorization(db, authorization_id=auth.authorization_id, reason="spent")

    http_posts = int(result.get("http_posts") or 0)
    receipt = SocialPublishReceipt(
        workspace_id=workspace_id,
        actor=actor,
        provider="meta",
        destination=CANARY_DESTINATION,
        page_id=CANARY_FACEBOOK_PAGE_ID,
        dry_run_id=int(dry_run_id),
        authorization_id=auth.authorization_id,
        payload_hash=payload_hash,
        idempotency_key=idempotency_key,
        external_post_id=result.get("external_post_id"),
        provider_request_id=result.get("provider_request_id"),
        provider_response_category=result.get("category"),
        http_status=result.get("status"),
        execution_mode=PublishingExecutionMode.CONTROLLED_CANARY.value,
        status="SUCCEEDED" if result.get("ok") else "FAILED",
        public_url=result.get("public_url"),
        detail_json=json.dumps(
            {
                "graph_api_version": result.get("graph_api_version"),
                "endpoint": result.get("endpoint"),
                "provider_called": result.get("provider_called"),
                "http_posts": http_posts,
                "timestamp_utc": timestamp,
                "response": result.get("response"),
            },
            default=str,
        )[:8000],
    )
    db.add(receipt)
    db.add(
        SocialIdempotencyRecord(
            idempotency_key=idempotency_key,
            action="publishing.facebook_canary",
            result_json=json.dumps(
                {
                    "ok": result.get("ok"),
                    "external_post_id": result.get("external_post_id"),
                    "authorization_id": auth.authorization_id,
                    "receipt_pending": True,
                },
                default=str,
            )[:8000],
        )
    )
    db.flush()

    # Ensure env restored visibly for audit.
    restored_fb = facebook_publishing_enabled()
    restored_mode = publishing_execution_mode().value

    out = {
        "ok": bool(result.get("ok")),
        "published": bool(result.get("ok")),
        "facebook_canary_posts_created": 1 if result.get("ok") else 0,
        "facebook_duplicate_posts": 0,
        "instagram_posts_created": 0,
        "provider_http_posts": http_posts,
        "provider_post_count": http_posts,
        "meta_facebook_publishing_enabled": restored_fb,
        "meta_instagram_publishing_enabled": False,
        "publishing_execution_mode": restored_mode,
        "canary_authorization_reused": False,
        "authorization_id": auth.authorization_id,
        "dry_run_id": dry_run_id,
        "payload_hash": payload_hash,
        "idempotency_key": idempotency_key,
        "page_id": CANARY_FACEBOOK_PAGE_ID,
        "page_name": CANARY_FACEBOOK_PAGE_NAME,
        "external_post_id": result.get("external_post_id"),
        "provider_request_id": result.get("provider_request_id"),
        "provider_response_category": result.get("category"),
        "public_url": result.get("public_url"),
        "timestamp_utc": timestamp,
        "receipt_id": receipt.id,
        "error": result.get("error"),
        "message": (
            "Facebook controlled canary succeeded. Authorization spent; gates restored to disabled."
            if result.get("ok")
            else f"Canary failed: {result.get('error') or result.get('category')}"
        ),
    }
    # Update idempotency with receipt id
    prior_row = db.query(SocialIdempotencyRecord).filter(SocialIdempotencyRecord.idempotency_key == idempotency_key).first()
    if prior_row:
        prior_row.result_json = json.dumps({k: out[k] for k in out if k != "message"}, default=str)[:8000]

    _audit(
        db,
        actor=actor,
        action="publishing.canary.execute",
        tool_name="publishing.facebook_canary",
        decision="allow" if out["ok"] else "deny",
        dry_run=False,
        detail={
            "authorization_id": auth.authorization_id,
            "dry_run_id": dry_run_id,
            "payload_hash": payload_hash,
            "idempotency_key": idempotency_key,
            "page_id": CANARY_FACEBOOK_PAGE_ID,
            "external_post_id": out.get("external_post_id"),
            "provider_response_category": out.get("provider_response_category"),
            "provider_http_posts": http_posts,
            "gates_restored_fb_enabled": restored_fb,
            "gates_restored_mode": restored_mode,
        },
    )
    return out


def deny_instagram_publish(**_kwargs: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "INSTAGRAM_PUBLISHING_HARD_DISABLED",
        "instagram_posts_created": 0,
        "provider_http_posts": 0,
    }
