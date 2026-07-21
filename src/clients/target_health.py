"""
Target health classifier.

Single source of truth used by the Scheduler API and UI to answer:
"Can this ChatTarget be sent to / joined / not at all?"

Returned codes (stable contract for the UI):

* ``sendable``   — used in merged health / scheduling when the target is treated
                   as directly postable (typically after membership lines up with
                   Telegram truth).
* ``joinable``   — has a public ``@username`` or a real ``joinchat/+hash`` invite
                   link. The auto-joiner can attempt to make the account a
                   member. After a successful join the target should become
                   ``sendable`` on the next save.
* ``needs_repair`` — has a cached ``tg_id`` but no public ``@username`` and no
                   real invite link; cannot safely auto-resolve or join. Quarantined
                   until the operator adds a handle or re-imports from joined groups.
* ``invalid``    — intrinsic field shape can never be sent to, regardless of
                   account or admin permissions. Examples:
                     * ``t.me/c/<channel_id>/<msg_id>`` — message link to a
                       private supergroup, not a join URL.
                     * ``t.me/<user>/<msg_id>`` — message permalink, not a
                       chat handle.
                     * row with no username, no invite, no tg_id.
                     * malformed username (too short / illegal chars).
                     * ``chat_type == 'user'`` (peer-user, not a chat target).

The classifier is pure and side-effect free. It must work whether ``t`` is a
SQLAlchemy ChatTarget instance or a plain dict (the API serializer uses the
same fields).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)


HEALTH_SENDABLE = "sendable"
HEALTH_JOINABLE = "joinable"
HEALTH_INVALID = "invalid"
# Operational / empirical (merged with intrinsic for API + executor honesty)
HEALTH_BANNED = "banned"
HEALTH_NO_PERMISSION = "no_permission"
HEALTH_UNRESOLVED_ENTITY = "unresolved_entity"
HEALTH_PENDING_APPROVAL = "pending_approval"
HEALTH_NEEDS_REPAIR = "needs_repair"

# Lower number = worse (used when merging intrinsic vs operational signals).
_HEALTH_RANK: Dict[str, int] = {
    HEALTH_INVALID: 0,
    HEALTH_BANNED: 1,
    HEALTH_NO_PERMISSION: 2,
    HEALTH_PENDING_APPROVAL: 2,
    HEALTH_NEEDS_REPAIR: 2,
    HEALTH_UNRESOLVED_ENTITY: 3,
    HEALTH_JOINABLE: 4,
    HEALTH_SENDABLE: 5,
}


# A Telegram username is 5-32 chars, alnum + underscore, must start with a letter.
# We allow the leading '@' to be present or absent.
_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")

# Message-link patterns we explicitly reject — these are NOT join targets:
#   https://t.me/c/2820391767/1            -> private supergroup message
#   https://t.me/<username>/12345          -> public message permalink
_MSG_LINK_PRIVATE_RE = re.compile(r"(?:^|/)c/\d+/\d+", re.IGNORECASE)
_MSG_LINK_PUBLIC_RE = re.compile(r"^https?://t\.me/[A-Za-z][A-Za-z0-9_]{3,31}/\d+", re.IGNORECASE)
# Same permalink shape without scheme (operators sometimes paste path-only into username).
_MSG_LINK_PUBLIC_PATH_RE = re.compile(
    r"^/?[A-Za-z][A-Za-z0-9_]{3,31}/\d+$",
    re.IGNORECASE,
)

# Real invite link shapes we accept:
#   https://t.me/joinchat/<HASH>
#   https://t.me/+<HASH>
#   bare "+HASH"
_INVITE_LINK_RE = re.compile(
    r"(t\.me/joinchat/[A-Za-z0-9_-]+|t\.me/\+[A-Za-z0-9_-]+|^\+[A-Za-z0-9_-]+$)",
    re.IGNORECASE,
)


def _get(row: Any, name: str) -> Optional[Any]:
    """Tolerant attr/key getter so we can classify both ORM rows and dicts."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _norm_str(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else (str(v).strip() if v is not None else "")


def _strip_invisible(s: str) -> str:
    """Remove bidi & zero-width marks so 'cryptodiscussing' is not mis-read by ASCII regex."""
    if not s:
        return s
    out: list = []
    for c in s:
        if c in "\u200b\u200c\u200d\ufeff":
            continue
        if unicodedata.category(c) == "Cf" and c not in "_":  # format chars (ZW*, etc.)
            continue
        out.append(c)
    return "".join(out)


def _field_looks_like_private_message_link(s: str) -> bool:
    """True if ``s`` contains a ``t.me/c/<id>/<msg>`` style fragment anywhere."""
    if not s:
        return False
    return bool(_MSG_LINK_PRIVATE_RE.search(s))


def _field_looks_like_public_message_link(s: str) -> bool:
    """
    Detect message permalinks pasted into username *or* invite fields.

    Accepts full URLs or bare ``channelname/12345`` paths.
    """
    if not s:
        return False
    t = s.strip()
    if _MSG_LINK_PUBLIC_RE.match(t):
        return True
    # Bare path often stored mistakenly as username
    if _MSG_LINK_PUBLIC_PATH_RE.match(t):
        return True
    if "t.me/" in t.lower():
        low = t.lower()
        idx = low.find("t.me/")
        tail = t[idx + 5 :].lstrip("/")
        if _MSG_LINK_PUBLIC_PATH_RE.match(tail):
            return True
    return False


def _username_looks_valid_public(username: str) -> bool:
    """
    Public Telegram handles are 5-32 of [A-Za-z0-9_], not mixing scripts.
    Apply invisible stripping first (operators paste from UI with ZW*).
    If UI injects a confusable script, try ASCII-only extraction for matching.
    """
    u = _strip_invisible(username).lstrip("@")
    if not u:
        return False
    if _USERNAME_RE.match(u):
        return True
    u_nfc = unicodedata.normalize("NFC", u)
    u_ascii = u_nfc.encode("ascii", "ignore").decode("ascii")
    return bool(u_ascii and _USERNAME_RE.match(u_ascii))


def classify_target(row: Any) -> Dict[str, str]:
    """
    Return ``{"health": <code>, "reason": <human-readable hint>}``.

    The reason is short — it's surfaced inline in the operator UI as a tooltip
    or muted helper text, not as a long error message.
    """
    if row is None:
        return {"health": HEALTH_INVALID, "reason": "Empty target row"}

    chat_type = _norm_str(_get(row, "chat_type")).lower()
    username = _norm_str(_get(row, "username"))
    invite_link = _norm_str(_get(row, "invite_link"))
    tg_id = _get(row, "tg_id")

    # `chat_type == "user"` usually means a DM, but some rows are mis-typed: a
    # real channel has a negative id (e.g. -100…). In that case keep classifying
    # as a normal target, not a PeerUser regression.
    if chat_type == "user":
        try:
            if tg_id is not None and int(tg_id) < 0:
                pass  # fall through
            else:
                return {
                    "health": HEALTH_INVALID,
                    "reason": "Direct-user peer is not a valid scheduler target",
                }
        except (TypeError, ValueError):
            return {
                "health": HEALTH_INVALID,
                "reason": "Direct-user peer is not a valid scheduler target",
            }

    # Reject obvious message links first — these are the most common noise.
    # Check *both* fields: operators often paste ``t.me/c/...`` into username by mistake.
    for field_name, field_val in (("invite_link", invite_link), ("username", username)):
        if not field_val:
            continue
        if _field_looks_like_private_message_link(field_val):
            return {
                "health": HEALTH_INVALID,
                "reason": "Private chat message link (t.me/c/...) — not a join URL. Use @username or a join invite.",
            }
        if _field_looks_like_public_message_link(field_val):
            return {
                "health": HEALTH_INVALID,
                "reason": "Message permalink — not a join target. Use the @username or invite link for the chat.",
            }

    # Prefer resolvable handles over raw tg_id. Cached tg_id with a valid @username
    # has caused PeerUser / wrong-entity sends — treat as joinable until proven.
    if invite_link and _INVITE_LINK_RE.search(invite_link):
        return {"health": HEALTH_JOINABLE,
                "reason": "Invite link present — auto-join required before first send"}

    if username and _username_looks_valid_public(username):
        return {
            "health": HEALTH_JOINABLE,
            "reason": "Public @username — auto-join required before first send",
        }
    if username:
        return {
            "health": HEALTH_INVALID,
            "reason": "Malformed username (5-32 [A-Za-z0-9_], Latin; invisible chars may be stripped)",
        }

    # Operator private self / Saved Messages: positive tg_id peer without @username.
    if chat_type == "private" and tg_id:
        try:
            if int(tg_id) > 0:
                return {
                    "health": HEALTH_SENDABLE,
                    "reason": "Private operator self target (Saved Messages / self peer)",
                }
        except (TypeError, ValueError):
            pass

    # tg_id without @username or invite — cannot safely resolve/join; quarantine.
    if tg_id:
        try:
            if int(tg_id) != 0:
                return {
                    "health": HEALTH_NEEDS_REPAIR,
                    "reason": "No @username or invite — only a cached chat id; add a handle or re-import from joined groups",
                }
        except (TypeError, ValueError):
            pass

    return {"health": HEALTH_INVALID, "reason": "No @username, invite link, or chat id on this target"}


def _health_rank(code: str) -> int:
    return _HEALTH_RANK.get(code, 4)


def parse_delivery_error_to_operational(
    error_message: Optional[str],
    error_code: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    Map ``MessageDelivery.error_message`` (and optional ``error_code``) to operational health.

    Returns ``(health_constant, short_reason)`` or None if not matched.
    """
    if not error_message:
        return None
    s = error_message.strip()
    low = s.lower()
    code_u = (error_code or "").strip().upper()

    if code_u in ("FLOODWAIT", "PEERFLOOD", "SLOWMODEWAIT"):
        return HEALTH_NO_PERMISSION, "Temporary Telegram rate limit / slow mode (recent send) — retry later, not a binding ban"
    if code_u in ("CHATGUESTSENDFORBIDDEN", "CHATSENDPLAINFORBIDDEN"):
        return HEALTH_NO_PERMISSION, "Post blocked by chat rules for this account role (recent send)"
    if code_u == "USERRESTRICTED":
        return HEALTH_NO_PERMISSION, "Telegram account restriction / spam block (recent send)"
    if code_u == "CHATRESTRICTED":
        return HEALTH_NO_PERMISSION, "Chat restricted for this account (recent send)"
    if code_u == "USERBANNEDINCHANNEL" and "false positive" in low:
        return (
            HEALTH_NO_PERMISSION,
            "Telegram USER_BANNED_IN_CHANNEL (executor notes possible post-join sync) — verify in the official app before treating as a ban",
        )
    if "userbannedinchannel" in low or "user banned in channel" in low:
        return HEALTH_BANNED, "Account is banned in this channel (recent send)"
    if "chatwriteforbidden" in low or "no permission to post" in low:
        return HEALTH_NO_PERMISSION, "No permission to post (recent send)"
    if "channelprivate" in low or "channel is private" in low:
        return HEALTH_NO_PERMISSION, "Channel private / no access (recent send)"
    if "peeruser" in low or "could not find the input entity" in low or "cannot find any entity" in low:
        return HEALTH_UNRESOLVED_ENTITY, "Entity resolution failed — wrong cached id or handle (recent send)"
    if "invitehashinvalid" in low or "usernameinvalid" in low or "username not occupied" in low:
        return HEALTH_INVALID, "Invalid username or invite (recent send)"
    if "entity resolution failed" in low:
        return HEALTH_UNRESOLVED_ENTITY, "Entity resolution failed (recent send)"
    if "join request" in low or "waiting for admin approval" in low or "pending approval" in low:
        return HEALTH_PENDING_APPROVAL, "Join request pending admin approval"
    return None


def derive_operational_health_from_db(
    db: Any,
    target_id: int,
    account_id: Optional[int] = None,
    *,
    lookback_days: int = 90,
    proven_send_days: int = 14,
) -> Tuple[Optional[Dict[str, str]], bool]:
    """
    Inspect recent ``MessageDelivery`` rows and ``AccountTargetBinding.can_post``.

    Returns ``(operational_dict_or_None, proven_send_recent)`` where
    ``operational_dict`` is ``{"health": ..., "reason": ...}`` if a downgrade
    applies, else None. ``proven_send_recent`` is True when the newest delivery
    in the window is a recent SENT.
    """
    from sqlalchemy import and_

    from src.core.scheduler_models import AccountTargetBinding, MessageDelivery

    def _st_upper(d: Any) -> str:
        s = getattr(d, "status", None)
        if s is None:
            return ""
        v = s.value if hasattr(s, "value") else s
        return str(v).strip().upper()

    tid = int(target_id)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)

    # Strict SQLAlchemy order: every WHERE clause via a single filter(and_(...))
    # before order_by / limit / all. Never chain .filter() onto a limited query.
    delivery_criteria = [
        MessageDelivery.target_id == tid,
        MessageDelivery.created_at >= since,
    ]
    if account_id is not None:
        delivery_criteria.append(MessageDelivery.account_id == int(account_id))
    q_delivery = (
        db.query(MessageDelivery)
        .filter(and_(*delivery_criteria))
        .order_by(MessageDelivery.created_at.desc())
        .limit(40)
    )
    logger.info(
        "target_health_query_built",
        component="message_delivery",
        target_id=tid,
        account_id=account_id,
        has_limit=getattr(q_delivery, "_limit_clause", None) is not None,
        has_filters=bool(getattr(q_delivery, "_where_criteria", None)),
    )
    rows: List[Any] = list(q_delivery.all())

    proven_send_recent = False
    operational: Optional[Dict[str, str]] = None
    worst_rank = 99

    if rows:
        newest = rows[0]
        st = _st_upper(newest)
        if st == "SENT" and newest.created_at:
            ca = newest.created_at
            if getattr(ca, "tzinfo", None):
                ca_utc = ca.astimezone(timezone.utc)
            else:
                ca_utc = ca.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ca_utc <= timedelta(days=proven_send_days):
                proven_send_recent = True
        if st == "FAILED":
            parsed = parse_delivery_error_to_operational(
                getattr(newest, "error_message", None),
                getattr(newest, "error_code", None),
            )
            if parsed:
                operational = {"health": parsed[0], "reason": parsed[1]}
                worst_rank = _health_rank(parsed[0])

    # Binding: no post permission (per-account or all accounts).
    # Fresh db.query() each time — never reuse a query object that may carry limit
    # state from another branch (defensive against session/query edge cases).
    if account_id is not None:
        q_bind = db.query(AccountTargetBinding).filter(
            and_(
                AccountTargetBinding.target_id == tid,
                AccountTargetBinding.account_id == int(account_id),
            )
        )
        logger.info(
            "target_health_query_built",
            component="account_target_binding",
            target_id=tid,
            account_id=account_id,
            has_limit=getattr(q_bind, "_limit_clause", None) is not None,
            has_filters=bool(getattr(q_bind, "_where_criteria", None)),
        )
        b = q_bind.first()
        if b and not bool(b.can_post):
            cand = {"health": HEALTH_NO_PERMISSION, "reason": "Binding disabled — no post permission for this account"}
            rnk = _health_rank(HEALTH_NO_PERMISSION)
            if rnk < worst_rank:
                worst_rank = rnk
                operational = cand
    else:
        q_bind_all = db.query(AccountTargetBinding).filter(AccountTargetBinding.target_id == tid)
        logger.info(
            "target_health_query_built",
            component="account_target_binding",
            target_id=tid,
            account_id=account_id,
            has_limit=getattr(q_bind_all, "_limit_clause", None) is not None,
            has_filters=bool(getattr(q_bind_all, "_where_criteria", None)),
        )
        binds = q_bind_all.all()
        if binds and all(not bool(x.can_post) for x in binds):
            cand = {
                "health": HEALTH_NO_PERMISSION,
                "reason": "All account bindings lack post permission for this target",
            }
            rnk = _health_rank(HEALTH_NO_PERMISSION)
            if rnk < worst_rank:
                worst_rank = rnk
                operational = cand

    return operational, proven_send_recent


def merge_intrinsic_and_operational(
    intrinsic: Dict[str, str],
    operational: Optional[Dict[str, str]],
) -> Dict[str, str]:
    """Pick the more restrictive health; keep reasons readable."""
    ih = intrinsic.get("health") or HEALTH_JOINABLE
    ir = intrinsic.get("reason") or ""
    if not operational:
        return {"health": ih, "health_reason": ir}
    oh = operational.get("health") or HEALTH_JOINABLE
    op_reason = operational.get("reason") or ""
    op_low = (op_reason or "").lower()
    # Stale ``PeerUser`` / wrong-id send failures after the row was repaired to prefer
    # @username (intrinsic joinable/sendable). Do not override stronger signals
    # (banned, no_permission, invalid, pending).
    if (
        oh == HEALTH_UNRESOLVED_ENTITY
        and ih in (HEALTH_JOINABLE, HEALTH_SENDABLE)
        and "peeruser" in op_low
        and "could not find the input entity" in op_low
    ):
        return {"health": ih, "health_reason": ir}
    if _health_rank(ih) <= _health_rank(oh):
        return {"health": ih, "health_reason": ir}
    return {"health": oh, "health_reason": op_reason}


def merged_target_health_row(
    db: Any,
    target_row: Any,
    account_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Full row fields for API: intrinsic + operational merge + proven-send flag.

    ``account_id`` scopes deliveries and binding checks to one account when set
    (Campaign Setup); omit for fleet-wide worst-case on the target.
    """
    intrinsic = classify_target(target_row)
    op, proven = derive_operational_health_from_db(db, int(_get(target_row, "id")), account_id)
    merged = merge_intrinsic_and_operational(intrinsic, op)
    return {
        "intrinsic_health": intrinsic.get("health"),
        "intrinsic_health_reason": intrinsic.get("reason"),
        "operational_health": op.get("health") if op else None,
        "operational_health_reason": op.get("reason") if op else None,
        "health": merged["health"],
        "health_reason": merged["health_reason"],
        "proven_send_recent": proven,
    }


def is_health_allowed_for_send(health: str) -> bool:
    """Executor / generator: only these may attempt Telegram delivery."""
    return health in (HEALTH_SENDABLE, HEALTH_JOINABLE)


def is_health_allowed_for_binding(health: str) -> bool:
    """API may still create a binding while a join request is pending."""
    return health in (HEALTH_SENDABLE, HEALTH_JOINABLE, HEALTH_PENDING_APPROVAL)


def entity_probe_chain(row: Any) -> List[Any]:
    """
    Ordered refs for Telethon ``get_entity`` probes.

    Prefer **@username** and **invite_link** before a bare numeric ``tg_id`` so a
    stale cached id can be bypassed when handles still resolve.
    """
    out: List[Any] = []
    seen: set = set()

    def add(v: Any) -> None:
        if v is None:
            return
        key = ("i", v) if isinstance(v, int) else ("s", str(v))
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    u_raw = _get(row, "username")
    u = _strip_invisible(_norm_str(u_raw)).lstrip("@")
    if u and _username_looks_valid_public(u_raw or u):
        add(u)
    inv = _norm_str(_get(row, "invite_link"))
    if inv:
        add(inv)
    tg_id = _get(row, "tg_id")
    if tg_id is not None:
        try:
            iv = int(tg_id)
            if iv != 0:
                add(iv)
        except (TypeError, ValueError):
            pass
    return out


def resolve_executor_entity(row: Any) -> Any:
    """
    Value to pass to Telethon ``send_message(..., entity=…)``.

    Prefer **@username** and **invite_link** over a raw numeric ``tg_id``. A cached
    ``tg_id`` may be a user id (PeerUser) or otherwise wrong while a username still
    resolves to the correct channel — matching the failure mode:
    ``Could not find the input entity for PeerUser(user_id=…)``.
    """
    username = _norm_str(_get(row, "username"))
    invite_link = _norm_str(_get(row, "invite_link"))
    tg_id = _get(row, "tg_id")

    if username and _username_looks_valid_public(username):
        return _strip_invisible(username).lstrip("@")
    if invite_link:
        return invite_link
    if tg_id is not None:
        try:
            v = int(tg_id)
            if v != 0:
                return v
        except (TypeError, ValueError):
            pass
    return None
