"""Canonical Meta connection services (UI and AI share these)."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from src.social_agent.credential_crypto import (
    CredentialCryptoConfigurationError,
    CredentialCryptoError,
    SocialCredentialCrypto,
)
from src.social_agent.models import SocialConnection, SocialOAuthState
from src.social_agent.providers.meta import MetaProviderAdapter, capability_matrix
from src.social_agent.services import _audit

STATE_TTL_SECONDS = 600
WORKSPACE_DEFAULT = "default"


def _publishing_blocked() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "UNSUPPORTED",
        "message": "Meta publishing is disabled in this phase (read-only OAuth).",
        "facebook_publishing_enabled": False,
        "instagram_publishing_enabled": False,
    }


def assert_meta_publishing_disabled() -> dict[str, Any]:
    fb = (os.environ.get("META_FACEBOOK_PUBLISHING_ENABLED") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    ig = (os.environ.get("META_INSTAGRAM_PUBLISHING_ENABLED") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if fb or ig:
        # Still refuse to publish from this module.
        return {
            **_publishing_blocked(),
            "error": "NOT_AUTHORIZED",
            "message": "Publishing flags must remain false; refuse mutation.",
        }
    return _publishing_blocked()


def _safe_destinations(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for p in pages:
        out.append(
            {
                "page_id": p.get("page_id"),
                "page_name": p.get("page_name"),
                "category": p.get("category"),
                "tasks": p.get("tasks") or [],
                "instagram_account_id": p.get("instagram_account_id"),
                "instagram_username": p.get("instagram_username"),
                "token_available": bool(p.get("page_access_token")),
            }
        )
    return out


def _public_connection(row: SocialConnection) -> dict[str, Any]:
    perms = []
    try:
        perms = json.loads(row.permissions_json or "[]")
    except json.JSONDecodeError:
        perms = []
    dests = []
    try:
        dests = json.loads(row.destinations_json or "[]")
    except json.JSONDecodeError:
        dests = []
    return {
        "id": row.id,
        "provider": row.provider,
        "display_name": row.display_name,
        "status": row.status,
        "health": row.health,
        "external_account_id": row.external_account_id,
        "selected_page_id": row.selected_page_id,
        "selected_instagram_id": row.selected_instagram_id,
        "permissions": perms,
        "missing_permissions": [
            c["required_permission"]
            for c in capability_matrix(perms)
            if c["capability"] in {"list_pages", "read_page_metadata", "discover_instagram"}
            and not c["granted"]
        ],
        "destinations": dests,
        "token_expires_at": row.token_expires_at.isoformat() + "Z" if row.token_expires_at else None,
        "last_checked_at": row.last_checked_at.isoformat() + "Z" if row.last_checked_at else None,
        "workspace_id": row.workspace_id,
    }


def get_meta_connection(db: Session, *, workspace_id: str = WORKSPACE_DEFAULT) -> dict[str, Any]:
    row = (
        db.query(SocialConnection)
        .filter(SocialConnection.provider == "meta", SocialConnection.workspace_id == workspace_id)
        .order_by(SocialConnection.id.desc())
        .first()
    )
    adapter = MetaProviderAdapter()
    cfg = adapter.config()
    crypto_ok = SocialCredentialCrypto.configured()
    connection_state = "CREDENTIALS_MISSING"
    if cfg["configured"] and crypto_ok:
        connection_state = "NOT_CONNECTED"
    elif cfg["configured"] and not crypto_ok:
        connection_state = "CREDENTIAL_KEY_MISSING"

    base = {
        "ok": True,
        "configured": bool(cfg["configured"]),
        "credential_encryption_configured": crypto_ok,
        "api_version": cfg["api_version"],
        "app_mode": cfg["app_mode"],
        "scopes_requested": cfg["scopes"],
        "facebook_publishing_enabled": False,
        "instagram_publishing_enabled": False,
        "connection": None,
        "connection_state": connection_state,
        "capabilities": capability_matrix(),
    }
    if not row:
        return base
    pub = _public_connection(row)
    base["connection"] = pub
    base["connection_state"] = "CONNECTED" if (row.status == "connected" or (row.health or "").upper() == "CONNECTED") else (row.health or row.status or "DEGRADED")
    base["capabilities"] = capability_matrix(pub.get("permissions") or [])
    return base


def start_meta_oauth(
    db: Session,
    *,
    actor: str,
    workspace_id: str = WORKSPACE_DEFAULT,
    purpose: str = "connect",
    connection_id: int | None = None,
    adapter: MetaProviderAdapter | None = None,
) -> dict[str, Any]:
    adapter = adapter or MetaProviderAdapter()
    cfg = adapter.config()
    if not cfg["configured"]:
        return {
            "ok": False,
            "error": "CREDENTIALS_MISSING",
            "meta": {
                "configured": False,
                "redirect_uri_present": bool(cfg.get("redirect_uri")),
                "api_version": cfg.get("api_version"),
                "scopes_requested": cfg.get("scopes"),
            },
        }
    if not SocialCredentialCrypto.configured():
        return {"ok": False, "error": "CREDENTIAL_KEY_MISSING", "message": "Install SOCIAL_CREDENTIAL_* keys before OAuth."}
    if purpose in {"reconnect", "publish_canary", "facebook_canary"} and connection_id is None:
        return {"ok": False, "error": "connection_id_required"}

    redirect = cfg["redirect_uri"]
    parsed = urlparse(redirect)
    if parsed.scheme != "https" and (os.environ.get("ENVIRONMENT") or "production").lower() in {
        "production",
        "prod",
    }:
        return {"ok": False, "error": "redirect_must_be_https"}

    state = secrets.token_urlsafe(32)
    row = SocialOAuthState(
        state=state,
        provider="meta",
        actor=actor,
        workspace_id=workspace_id,
        redirect_uri=redirect,
        purpose=purpose,
        connection_id=connection_id,
        expires_at=datetime.utcnow() + timedelta(seconds=STATE_TTL_SECONDS),
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        actor=actor,
        action="meta.connection_started" if purpose == "connect" else "meta.reconnect_started",
        decision="allow",
        detail={"workspace_id": workspace_id, "purpose": purpose, "connection_id": connection_id},
    )
    try:
        url = adapter.authorization_url(state=state, purpose=purpose)
    except Exception:
        return {"ok": False, "error": "authorization_url_failed"}
    return {
        "ok": True,
        "authorization_url": url,
        "expires_in": STATE_TTL_SECONDS,
        "purpose": purpose,
        "scopes_requested": (
            adapter.config().get("canary_scopes")
            if purpose in {"publish_canary", "facebook_canary"}
            else adapter.config().get("scopes")
        ),
    }


def _consume_state(
    db: Session,
    *,
    state: str | None,
    actor: str,
    workspace_id: str,
) -> tuple[SocialOAuthState | None, str | None]:
    if not state:
        return None, "missing_state"
    row = db.query(SocialOAuthState).filter(SocialOAuthState.state == state).first()
    if row is None:
        return None, "invalid_state"
    if row.provider != "meta":
        return None, "wrong_provider"
    if row.actor != actor:
        return None, "wrong_user"
    if row.workspace_id != workspace_id:
        return None, "wrong_workspace"
    if row.used_at is not None:
        return None, "reused_state"
    if row.expires_at < datetime.utcnow():
        return None, "expired_state"
    row.used_at = datetime.utcnow()
    db.flush()
    return row, None


def handle_meta_callback(
    db: Session,
    *,
    actor: str,
    workspace_id: str = WORKSPACE_DEFAULT,
    state: str | None,
    code: str | None,
    error: str | None = None,
    error_description: str | None = None,
    adapter: MetaProviderAdapter | None = None,
) -> dict[str, Any]:
    adapter = adapter or MetaProviderAdapter()
    oauth_row, state_err = _consume_state(db, state=state, actor=actor, workspace_id=workspace_id)
    if state_err:
        _audit(
            db,
            actor=actor,
            action="meta.callback_rejected",
            decision="deny",
            detail={"reason": state_err, "workspace_id": workspace_id},
        )
        return {"ok": False, "error": state_err}

    if error:
        _audit(
            db,
            actor=actor,
            action="meta.callback_rejected",
            decision="deny",
            detail={"reason": "provider_error", "error": str(error)[:120]},
        )
        return {"ok": False, "error": "provider_denied", "message": (error_description or error)[:200]}

    if not code:
        _audit(db, actor=actor, action="meta.callback_rejected", decision="deny", detail={"reason": "missing_code"})
        return {"ok": False, "error": "missing_code"}

    exchanged = adapter.exchange_code(code=code)
    if not exchanged.get("ok"):
        _audit(
            db,
            actor=actor,
            action="meta.callback_rejected",
            decision="deny",
            detail={"reason": "token_exchange_failed", "category": exchanged.get("category")},
        )
        return {"ok": False, "error": "token_exchange_failed"}

    token = exchanged["access_token"]
    long_lived = adapter.exchange_long_lived(short_lived_token=token)
    if long_lived.get("ok"):
        token = long_lived["access_token"]
        expires_in = long_lived.get("expires_in")
    else:
        expires_in = exchanged.get("expires_in")

    me = adapter.get_me(access_token=token)
    if not me.get("ok"):
        return {"ok": False, "error": "user_lookup_failed", "category": me.get("category")}

    debug = adapter.debug_token(input_token=token)
    granted: list[str] = []
    if debug.get("ok"):
        scopes = (debug.get("data") or {}).get("scopes") or []
        if isinstance(scopes, list):
            granted = [str(s) for s in scopes]

    pages_result = adapter.list_pages(access_token=token)
    if not pages_result.get("ok"):
        return {"ok": False, "error": "pages_discovery_failed", "category": pages_result.get("category")}

    pages = pages_result["pages"]
    # Enrich Instagram via page token when list omitted details
    for p in pages:
        if p.get("page_access_token") and not p.get("instagram_account_id"):
            ig = adapter.page_instagram(page_id=p["page_id"], page_access_token=p["page_access_token"])
            if ig.get("ok") and ig.get("instagram"):
                p["instagram_account_id"] = ig["instagram"]["instagram_account_id"]
                p["instagram_username"] = ig["instagram"].get("username")

    try:
        crypto = SocialCredentialCrypto.from_environment()
    except CredentialCryptoConfigurationError:
        return {"ok": False, "error": "CREDENTIAL_KEY_MISSING"}

    page_token_map = {p["page_id"]: p.get("page_access_token") for p in pages if p.get("page_id")}
    credential_payload = {
        "token_type": exchanged.get("token_type") or "bearer",
        "user_access_token": token,
        "page_access_tokens": page_token_map,
        "meta_user_id": me["id"],
    }
    try:
        envelope = crypto.encrypt_json(credential_payload)
    except CredentialCryptoError:
        return {"ok": False, "error": "encrypt_failed"}

    expires_at = None
    if expires_in:
        try:
            expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            expires_at = None

    safe_dests = _safe_destinations(pages)
    display = me.get("name") or f"Meta user {me['id']}"

    # reconnect / canary: update existing; connect: upsert workspace meta connection
    target: SocialConnection | None = None
    if oauth_row and oauth_row.purpose in {"reconnect", "publish_canary", "facebook_canary"} and oauth_row.connection_id:
        target = db.query(SocialConnection).filter(SocialConnection.id == int(oauth_row.connection_id)).first()
    if target is None:
        target = (
            db.query(SocialConnection)
            .filter(SocialConnection.provider == "meta", SocialConnection.workspace_id == workspace_id)
            .order_by(SocialConnection.id.desc())
            .first()
        )
    prev_page = target.selected_page_id if target else None
    prev_ig = target.selected_instagram_id if target else None
    prev_display = target.display_name if target else None
    if target is None:
        target = SocialConnection(provider="meta", workspace_id=workspace_id, created_by=actor)
        db.add(target)

    target.external_account_id = me["id"]
    target.status = "connected_pending_selection" if len(safe_dests) != 1 else "connected"
    target.health = "CONNECTED"
    target.permissions_json = json.dumps(granted)
    target.credentials_encrypted = envelope
    target.destinations_json = json.dumps(safe_dests)
    target.token_expires_at = expires_at
    target.last_checked_at = datetime.utcnow()
    target.created_by = actor

    if len(safe_dests) == 1:
        only = safe_dests[0]
        target.selected_page_id = only.get("page_id")
        target.selected_instagram_id = only.get("instagram_account_id")
        target.display_name = only.get("page_name") or display
        if not only.get("instagram_account_id"):
            target.status = "connected_no_instagram"
        else:
            target.status = "connected"
    elif prev_page and any(str(d.get("page_id")) == str(prev_page) for d in safe_dests):
        # Preserve prior Page selection across canary/reconnect (e.g. Exswaping).
        target.selected_page_id = prev_page
        target.selected_instagram_id = prev_ig
        match = next((d for d in safe_dests if str(d.get("page_id")) == str(prev_page)), None)
        target.display_name = (match or {}).get("page_name") or prev_display or display
        target.status = "connected"
    else:
        target.display_name = display


    db.flush()
    _audit(
        db,
        actor=actor,
        action="meta.callback_accepted",
        decision="allow",
        detail={
            "connection_id": target.id,
            "pages_discovered": len(safe_dests),
            "instagram_accounts_discovered": sum(1 for d in safe_dests if d.get("instagram_account_id")),
            "granted_permission_count": len(granted),
        },
    )
    _audit(
        db,
        actor=actor,
        action="meta.token_exchanged",
        decision="allow",
        detail={"connection_id": target.id, "long_lived": bool(long_lived.get("ok"))},
    )
    _audit(
        db,
        actor=actor,
        action="meta.pages_discovered",
        decision="allow",
        detail={"connection_id": target.id, "count": len(safe_dests)},
    )
    if oauth_row and oauth_row.purpose in {"reconnect", "publish_canary", "facebook_canary"}:
        _audit(
            db,
            actor=actor,
            action="meta.reconnect_completed" if oauth_row.purpose == "reconnect" else "meta.canary_oauth_completed",
            decision="allow",
            detail={"connection_id": target.id, "purpose": oauth_row.purpose, "pages_manage_posts": "pages_manage_posts" in granted},
        )

    return {
        "ok": True,
        "connection": _public_connection(target),
        "pages_discovered": len(safe_dests),
        "instagram_accounts_discovered": sum(1 for d in safe_dests if d.get("instagram_account_id")),
        "needs_page_selection": len(safe_dests) > 1,
        "provider_mutations": 0,
    }


def select_meta_destination(
    db: Session,
    *,
    actor: str,
    connection_id: int,
    page_id: str,
    instagram_account_id: str | None = None,
    workspace_id: str = WORKSPACE_DEFAULT,
) -> dict[str, Any]:
    row = (
        db.query(SocialConnection)
        .filter(
            SocialConnection.id == int(connection_id),
            SocialConnection.provider == "meta",
            SocialConnection.workspace_id == workspace_id,
        )
        .first()
    )
    if not row:
        return {"ok": False, "error": "not_found"}
    try:
        dests = json.loads(row.destinations_json or "[]")
    except json.JSONDecodeError:
        dests = []
    match = next((d for d in dests if str(d.get("page_id")) == str(page_id)), None)
    if not match:
        return {"ok": False, "error": "page_not_in_discovered_set"}
    ig = instagram_account_id if instagram_account_id is not None else match.get("instagram_account_id")
    if ig and match.get("instagram_account_id") and str(ig) != str(match.get("instagram_account_id")):
        return {"ok": False, "error": "instagram_not_linked_to_page"}
    row.selected_page_id = str(page_id)
    row.selected_instagram_id = str(ig) if ig else None
    row.status = "connected" if ig else "connected_no_instagram"
    row.updated_at = datetime.utcnow()
    db.flush()
    _audit(
        db,
        actor=actor,
        action="meta.destination_selected",
        decision="allow",
        detail={
            "connection_id": row.id,
            "page_id": row.selected_page_id,
            "instagram_selected": bool(row.selected_instagram_id),
        },
    )
    return {"ok": True, "connection": _public_connection(row)}


def meta_health_check(
    db: Session,
    *,
    actor: str,
    connection_id: int | None = None,
    workspace_id: str = WORKSPACE_DEFAULT,
    adapter: MetaProviderAdapter | None = None,
) -> dict[str, Any]:
    adapter = adapter or MetaProviderAdapter()
    q = db.query(SocialConnection).filter(
        SocialConnection.provider == "meta", SocialConnection.workspace_id == workspace_id
    )
    if connection_id is not None:
        q = q.filter(SocialConnection.id == int(connection_id))
    row = q.order_by(SocialConnection.id.desc()).first()
    if not row:
        return {"ok": True, "health": "NOT_CONFIGURED", "connection": None}
    if not row.credentials_encrypted:
        row.health = "ERROR"
        return {"ok": False, "health": "ERROR", "error": "missing_credentials"}

    try:
        crypto = SocialCredentialCrypto.from_environment()
        payload = crypto.decrypt_json(row.credentials_encrypted)
    except CredentialCryptoConfigurationError:
        return {"ok": False, "health": "ERROR", "error": "CREDENTIAL_KEY_MISSING"}
    except CredentialCryptoError:
        row.health = "ERROR"
        return {"ok": False, "health": "ERROR", "error": "decrypt_failed"}

    token = payload.get("user_access_token")
    if not token:
        row.health = "ERROR"
        return {"ok": False, "health": "ERROR", "error": "token_missing"}

    if row.token_expires_at and row.token_expires_at < datetime.utcnow():
        row.health = "EXPIRED"
        row.last_checked_at = datetime.utcnow()
        _audit(db, actor=actor, action="meta.connection_degraded", decision="warn", detail={"health": "EXPIRED"})
        return {"ok": True, "health": "EXPIRED", "connection": _public_connection(row)}

    probe = adapter.health_probe(access_token=token, page_id=row.selected_page_id)
    health = probe.get("health") or "ERROR"
    if row.selected_instagram_id and health == "CONNECTED" and row.selected_page_id:
        page_tokens = payload.get("page_access_tokens") or {}
        page_token = page_tokens.get(row.selected_page_id) or token
        ig = adapter.page_instagram(page_id=row.selected_page_id, page_access_token=page_token)
        if not ig.get("ok"):
            health = "INSTAGRAM_UNAVAILABLE"
        elif not ig.get("instagram"):
            health = "INSTAGRAM_UNAVAILABLE"

    row.health = health
    row.last_checked_at = datetime.utcnow()
    if health != "CONNECTED":
        row.status = "degraded" if health not in {"EXPIRED", "REVOKED"} else health.lower()
        _audit(
            db,
            actor=actor,
            action="meta.connection_degraded",
            decision="warn",
            detail={"health": health, "connection_id": row.id},
        )
    else:
        if row.status.startswith("connected"):
            pass
        else:
            row.status = "connected"
    _audit(
        db,
        actor=actor,
        action="meta.health_checked",
        decision="allow",
        detail={"connection_id": row.id, "health": health},
    )
    return {"ok": True, "health": health, "connection": _public_connection(row), "provider_mutations": 0}


def disconnect_meta(
    db: Session,
    *,
    actor: str,
    connection_id: int,
    workspace_id: str = WORKSPACE_DEFAULT,
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        return {
            "ok": False,
            "error": "confirmation_required",
            "confirmation": {
                "action": "meta.disconnect",
                "connection_id": connection_id,
                "warning": "Removes local encrypted credentials. Remote Meta token revocation is separate.",
            },
        }
    row = (
        db.query(SocialConnection)
        .filter(
            SocialConnection.id == int(connection_id),
            SocialConnection.provider == "meta",
            SocialConnection.workspace_id == workspace_id,
        )
        .first()
    )
    if not row:
        return {"ok": False, "error": "not_found"}
    row.credentials_encrypted = None
    row.status = "disconnected"
    row.health = "NOT_CONFIGURED"
    row.selected_page_id = None
    row.selected_instagram_id = None
    row.destinations_json = "[]"
    row.token_expires_at = None
    row.last_checked_at = datetime.utcnow()
    db.flush()
    _audit(
        db,
        actor=actor,
        action="meta.disconnected",
        decision="allow",
        detail={"connection_id": row.id, "remote_revoke": "not_attempted"},
    )
    return {"ok": True, "connection": _public_connection(row), "remote_revoke": "not_attempted"}
