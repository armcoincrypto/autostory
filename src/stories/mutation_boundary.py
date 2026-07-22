"""Canonical Story mutation boundary (Phase 1.1).

Fail-closed controls for every Telegram Story publication path.

Layers:
1. ``STORY_MUTATIONS_ENABLED`` — global kill switch (default deny)
2. ``STORY_EXECUTION_MODE`` — disabled | dry-run | controlled-canary | live
3. Account allowlist — empty deny
4. Trigger authorization
5. Short-lived single-use provider authorization tokens
6. Provider-call wrapper that refuses ``SendStoryRequest`` without a token
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

STORY_MUTATIONS_FLAG = "STORY_MUTATIONS_ENABLED"
STORY_EXECUTION_MODE_FLAG = "STORY_EXECUTION_MODE"
STORY_ACCOUNT_ALLOWLIST_FLAG = "STORY_ACCOUNT_MUTATION_ALLOWLIST"
STORY_MUTATION_HMAC_FLAG = "STORY_MUTATION_AUTH_HMAC_KEY"

_TRUE = frozenset({"1", "true", "yes", "on"})
_TOKEN_TTL_SEC = 60
_PROVIDER_CALL_COUNT = 0
_PROVIDER_CALL_LOCK = threading.Lock()
_TOKEN_LOCK = threading.Lock()
_CONSUMED_TOKENS: set[str] = set()
_ISSUED_TOKENS: dict[str, "StoryMutationAuthorization"] = {}
_AUDIT_LOG: list[dict[str, Any]] = []


class StoryExecutionMode(str, Enum):
    DISABLED = "disabled"
    DRY_RUN = "dry-run"
    CONTROLLED_CANARY = "controlled-canary"
    LIVE = "live"


class StoryMutationTrigger(str, Enum):
    SCHEDULER = "scheduler"
    API = "api"
    OPERATOR = "operator"
    CANARY = "canary"
    AUTOMATION = "automation"
    RECOVERY = "recovery"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StoryMutationAuthorization:
    """Short-lived, single-use authorization for one provider Story mutation."""

    token_id: str
    account_id: int
    trigger: str
    execution_mode: str
    scope: str
    issued_at_unix: float
    expires_at_unix: float
    signature: str
    dry_run: bool = False
    idempotency_key: str | None = None

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at_unix


@dataclass
class StoryMutationDecision:
    allowed: bool
    decision: str
    denial_reason: str | None
    authorization: StoryMutationAuthorization | None = None
    audit: dict[str, Any] = field(default_factory=dict)
    provider_called: bool = False
    external_id: Any = None

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "denial_reason": self.denial_reason,
            "provider_called": self.provider_called,
            "external_id": self.external_id,
            **self.audit,
        }


def reset_provider_call_counter() -> None:
    global _PROVIDER_CALL_COUNT
    with _PROVIDER_CALL_LOCK:
        _PROVIDER_CALL_COUNT = 0


def get_provider_call_count() -> int:
    with _PROVIDER_CALL_LOCK:
        return _PROVIDER_CALL_COUNT


def _bump_provider_call_count() -> int:
    global _PROVIDER_CALL_COUNT
    with _PROVIDER_CALL_LOCK:
        _PROVIDER_CALL_COUNT += 1
        return _PROVIDER_CALL_COUNT


def clear_mutation_token_state_for_tests() -> None:
    with _TOKEN_LOCK:
        _CONSUMED_TOKENS.clear()
        _ISSUED_TOKENS.clear()
    _AUDIT_LOG.clear()
    reset_provider_call_counter()


def _env_truthy(name: str, *, default: str = "false") -> bool:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    value = str(raw).strip().lower()
    if value == "":
        return False
    if value in _TRUE:
        return True
    # malformed / unknown → deny
    if value in {"0", "false", "no", "off"}:
        return False
    return False


def story_mutations_enabled() -> bool:
    """Global kill switch. Missing or malformed → False."""
    return _env_truthy(STORY_MUTATIONS_FLAG, default="false")


def parse_story_execution_mode() -> StoryExecutionMode:
    raw = (os.environ.get(STORY_EXECUTION_MODE_FLAG) or "disabled").strip().lower()
    aliases = {
        "disabled": StoryExecutionMode.DISABLED,
        "off": StoryExecutionMode.DISABLED,
        "false": StoryExecutionMode.DISABLED,
        "dry-run": StoryExecutionMode.DRY_RUN,
        "dry_run": StoryExecutionMode.DRY_RUN,
        "dryrun": StoryExecutionMode.DRY_RUN,
        "controlled-canary": StoryExecutionMode.CONTROLLED_CANARY,
        "controlled_canary": StoryExecutionMode.CONTROLLED_CANARY,
        "canary": StoryExecutionMode.CONTROLLED_CANARY,
        "controlled_live": StoryExecutionMode.CONTROLLED_CANARY,
        "live": StoryExecutionMode.LIVE,
    }
    return aliases.get(raw, StoryExecutionMode.DISABLED)


def story_account_mutation_allowed(account_id: int | None) -> tuple[bool, str]:
    if account_id is None:
        return False, "account_id_required"
    raw = (os.environ.get(STORY_ACCOUNT_ALLOWLIST_FLAG) or "").strip()
    if not raw:
        return False, "account_mutation_allowlist_empty"
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    if str(int(account_id)) not in allowed:
        return False, "account_mutation_denied"
    return True, "account_mutation_allowed"


def _hmac_key() -> bytes:
    configured = (os.environ.get(STORY_MUTATION_HMAC_FLAG) or "").strip()
    if configured:
        return configured.encode("utf-8")
    # Process-local ephemeral key — tokens cannot survive restart (fail closed for reuse).
    existing = getattr(_hmac_key, "_ephemeral", None)
    if existing is None:
        existing = secrets.token_bytes(32)
        setattr(_hmac_key, "_ephemeral", existing)
    return existing


def _sign(token_id: str, account_id: int, expires_at_unix: float, mode: str, trigger: str) -> str:
    payload = f"{token_id}|{account_id}|{expires_at_unix:.0f}|{mode}|{trigger}"
    return hmac.new(_hmac_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _record_audit(entry: dict[str, Any]) -> None:
    entry = {
        **entry,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _AUDIT_LOG.append(entry)
    logger.info("story_mutation_audit", **{k: v for k, v in entry.items() if k != "authorization"})


def get_mutation_audit_log() -> list[dict[str, Any]]:
    return list(_AUDIT_LOG)


class StoryMutationService:
    """Single authorization authority for Story provider mutations."""

    @staticmethod
    def evaluate(
        *,
        account_id: int | None,
        trigger: StoryMutationTrigger | str,
        scope: str | None,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        caller: str = "unknown",
        service: str = "unknown",
        release_sha: str | None = None,
        pid: int | None = None,
        skip_legacy_purpose_check: bool = False,
    ) -> StoryMutationDecision:
        attempt_id = str(uuid.uuid4())
        trigger_s = (
            trigger.value if isinstance(trigger, StoryMutationTrigger) else str(trigger or "unknown")
        )
        mode = parse_story_execution_mode()
        global_on = story_mutations_enabled()
        account_ok, account_reason = story_account_mutation_allowed(account_id)
        audit_base: dict[str, Any] = {
            "attempt_id": attempt_id,
            "account_id": account_id,
            "trigger": trigger_s,
            "caller": caller,
            "service": service,
            "release_sha": release_sha,
            "pid": pid or os.getpid(),
            "execution_mode": mode.value,
            "global_switch": global_on,
            "account_permission": account_ok,
            "scope": scope,
            "idempotency_key": idempotency_key,
            "dry_run_requested": dry_run,
        }

        if dry_run or mode == StoryExecutionMode.DRY_RUN:
            decision = StoryMutationDecision(
                allowed=True,
                decision="dry-run",
                denial_reason=None,
                authorization=None,
                audit={**audit_base, "provider_called": False},
                provider_called=False,
            )
            _record_audit({**decision.to_audit_dict(), "result": "dry_run_no_provider"})
            return decision

        if not global_on:
            decision = StoryMutationDecision(
                allowed=False,
                decision="deny",
                denial_reason="story_mutations_disabled",
                audit=audit_base,
            )
            _record_audit({**decision.to_audit_dict(), "result": "denied"})
            return decision

        if mode == StoryExecutionMode.DISABLED:
            decision = StoryMutationDecision(
                allowed=False,
                decision="deny",
                denial_reason="story_execution_mode_disabled",
                audit=audit_base,
            )
            _record_audit({**decision.to_audit_dict(), "result": "denied"})
            return decision

        if mode not in (
            StoryExecutionMode.CONTROLLED_CANARY,
            StoryExecutionMode.LIVE,
        ):
            decision = StoryMutationDecision(
                allowed=False,
                decision="deny",
                denial_reason="story_execution_mode_unknown_or_unsupported",
                audit=audit_base,
            )
            _record_audit({**decision.to_audit_dict(), "result": "denied"})
            return decision

        if not account_ok:
            decision = StoryMutationDecision(
                allowed=False,
                decision="deny",
                denial_reason=account_reason,
                audit=audit_base,
            )
            _record_audit({**decision.to_audit_dict(), "result": "denied"})
            return decision

        # Trigger gates
        if trigger_s == StoryMutationTrigger.SCHEDULER.value:
            decision = StoryMutationDecision(
                allowed=False,
                decision="deny",
                denial_reason="scheduler_story_trigger_locked",
                audit=audit_base,
            )
            _record_audit({**decision.to_audit_dict(), "result": "denied"})
            return decision
        if trigger_s in {
            StoryMutationTrigger.AUTOMATION.value,
            StoryMutationTrigger.RECOVERY.value,
            StoryMutationTrigger.UNKNOWN.value,
        }:
            decision = StoryMutationDecision(
                allowed=False,
                decision="deny",
                denial_reason=f"trigger_{trigger_s}_denied",
                audit=audit_base,
            )
            _record_audit({**decision.to_audit_dict(), "result": "denied"})
            return decision

        if mode == StoryExecutionMode.CONTROLLED_CANARY and trigger_s not in {
            StoryMutationTrigger.CANARY.value,
            StoryMutationTrigger.OPERATOR.value,
            StoryMutationTrigger.API.value,
        }:
            decision = StoryMutationDecision(
                allowed=False,
                decision="deny",
                denial_reason="controlled_canary_trigger_mismatch",
                audit=audit_base,
            )
            _record_audit({**decision.to_audit_dict(), "result": "denied"})
            return decision

        if not skip_legacy_purpose_check:
            from src.stories.scheduler_integration import (
                controlled_story_execution_allowed,
                scheduler_story_execution_enabled,
            )

            if scope == "controlled_live":
                allowed, reason = controlled_story_execution_allowed(account_id)
                if not allowed:
                    decision = StoryMutationDecision(
                        allowed=False,
                        decision="deny",
                        denial_reason=reason,
                        audit=audit_base,
                    )
                    _record_audit({**decision.to_audit_dict(), "result": "denied"})
                    return decision
                if mode not in (
                    StoryExecutionMode.CONTROLLED_CANARY,
                    StoryExecutionMode.LIVE,
                ):
                    decision = StoryMutationDecision(
                        allowed=False,
                        decision="deny",
                        denial_reason="mode_incompatible_with_controlled_live",
                        audit=audit_base,
                    )
                    _record_audit({**decision.to_audit_dict(), "result": "denied"})
                    return decision
            elif scope == "scheduler":
                if not scheduler_story_execution_enabled():
                    decision = StoryMutationDecision(
                        allowed=False,
                        decision="deny",
                        denial_reason="scheduler_story_execution_disabled",
                        audit=audit_base,
                    )
                    _record_audit({**decision.to_audit_dict(), "result": "denied"})
                    return decision
                # Scheduler never certified for live mutation in this phase.
                decision = StoryMutationDecision(
                    allowed=False,
                    decision="deny",
                    denial_reason="scheduler_story_execution_not_certified",
                    audit=audit_base,
                )
                _record_audit({**decision.to_audit_dict(), "result": "denied"})
                return decision
            else:
                decision = StoryMutationDecision(
                    allowed=False,
                    decision="deny",
                    denial_reason="story_execution_purpose_required",
                    audit=audit_base,
                )
                _record_audit({**decision.to_audit_dict(), "result": "denied"})
                return decision

        assert account_id is not None
        issued = time.time()
        token_id = secrets.token_hex(16)
        expires = issued + _TOKEN_TTL_SEC
        signature = _sign(token_id, int(account_id), expires, mode.value, trigger_s)
        auth = StoryMutationAuthorization(
            token_id=token_id,
            account_id=int(account_id),
            trigger=trigger_s,
            execution_mode=mode.value,
            scope=str(scope or ""),
            issued_at_unix=issued,
            expires_at_unix=expires,
            signature=signature,
            dry_run=False,
            idempotency_key=idempotency_key,
        )
        with _TOKEN_LOCK:
            _ISSUED_TOKENS[token_id] = auth

        decision = StoryMutationDecision(
            allowed=True,
            decision="allow",
            denial_reason=None,
            authorization=auth,
            audit={**audit_base, "approval_id": token_id},
        )
        _record_audit({**decision.to_audit_dict(), "result": "authorized"})
        return decision

    @staticmethod
    def invalidate_pending_authorizations(*, reason: str = "global_disable") -> int:
        with _TOKEN_LOCK:
            n = len(_ISSUED_TOKENS)
            for token_id in list(_ISSUED_TOKENS):
                _CONSUMED_TOKENS.add(token_id)
                _ISSUED_TOKENS.pop(token_id, None)
        logger.warning("story_mutation_authorizations_invalidated", count=n, reason=reason)
        return n

    @staticmethod
    def consume_provider_authorization(
        authorization: StoryMutationAuthorization | None,
        *,
        account_id: int,
        allow_dry_run_token: bool = False,
    ) -> tuple[bool, str]:
        """Call-time recheck: global switch, expiry, signature, single-use."""
        if authorization is None:
            return False, "provider_authorization_missing"
        if authorization.dry_run and not allow_dry_run_token:
            return False, "dry_run_authorization_cannot_mutate"
        if not story_mutations_enabled():
            StoryMutationService.invalidate_pending_authorizations(reason="disabled_at_consume")
            return False, "story_mutations_disabled_at_provider"
        if authorization.expired:
            return False, "provider_authorization_expired"
        if int(authorization.account_id) != int(account_id):
            return False, "provider_authorization_account_mismatch"
        expected = _sign(
            authorization.token_id,
            int(authorization.account_id),
            authorization.expires_at_unix,
            authorization.execution_mode,
            authorization.trigger,
        )
        if not hmac.compare_digest(expected, authorization.signature):
            return False, "provider_authorization_forged"
        with _TOKEN_LOCK:
            if authorization.token_id in _CONSUMED_TOKENS:
                return False, "provider_authorization_reused"
            if authorization.token_id not in _ISSUED_TOKENS:
                return False, "provider_authorization_unknown"
            _CONSUMED_TOKENS.add(authorization.token_id)
            _ISSUED_TOKENS.pop(authorization.token_id, None)
        return True, "provider_authorization_ok"


async def invoke_send_story(
    client: Any,
    request: Any,
    *,
    authorization: StoryMutationAuthorization | None,
    account_id: int,
) -> Any:
    """Sole production entry for Telegram Story mutation requests."""
    from telethon.tl.functions.stories import SendStoryRequest

    if type(request) is not SendStoryRequest:
        raise TypeError("invoke_send_story only accepts SendStoryRequest")

    ok, reason = StoryMutationService.consume_provider_authorization(
        authorization, account_id=int(account_id)
    )
    if not ok:
        _record_audit(
            {
                "decision": "deny",
                "denial_reason": reason,
                "provider_called": False,
                "account_id": account_id,
                "result": "provider_boundary_denied",
            }
        )
        raise PermissionError(reason)

    _bump_provider_call_count()
    _record_audit(
        {
            "decision": "allow",
            "denial_reason": None,
            "provider_called": True,
            "account_id": account_id,
            "approval_id": authorization.token_id if authorization else None,
            "result": "provider_invoked",
        }
    )
    return await client(request)


def require_story_mutation_authorization(
    *,
    account_id: int | None,
    scope: str | None,
    trigger: str,
    caller: str,
) -> StoryMutationDecision:
    """Helper used by publishers immediately before provider invocation."""
    return StoryMutationService.evaluate(
        account_id=account_id,
        trigger=trigger,
        scope=scope,
        dry_run=False,
        caller=caller,
        service=os.environ.get("AUTOSTORY_SERVICE_NAME", "autostory"),
        release_sha=(os.environ.get("AUTOSTORY_RELEASE_SHA") or "")[:40] or None,
    )
