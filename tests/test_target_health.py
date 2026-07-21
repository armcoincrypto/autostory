"""Tests for target health classifier (scheduler operator guardrails).

Loads ``target_health`` by file path so importing ``src.clients`` (which pulls
Telethon) is not required in lightweight CI environments.
"""
import importlib.util
from pathlib import Path

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "target_health",
    _here.parent / "src" / "clients" / "target_health.py",
)
_target_health = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_target_health)

classify_target = _target_health.classify_target
resolve_executor_entity = _target_health.resolve_executor_entity
HEALTH_INVALID = _target_health.HEALTH_INVALID
HEALTH_JOINABLE = _target_health.HEALTH_JOINABLE
HEALTH_SENDABLE = _target_health.HEALTH_SENDABLE
HEALTH_NEEDS_REPAIR = _target_health.HEALTH_NEEDS_REPAIR
HEALTH_BANNED = _target_health.HEALTH_BANNED
HEALTH_NO_PERMISSION = _target_health.HEALTH_NO_PERMISSION
HEALTH_UNRESOLVED_ENTITY = _target_health.HEALTH_UNRESOLVED_ENTITY
parse_delivery_error_to_operational = _target_health.parse_delivery_error_to_operational
merge_intrinsic_and_operational = _target_health.merge_intrinsic_and_operational
is_health_allowed_for_binding = _target_health.is_health_allowed_for_binding
is_health_allowed_for_send = _target_health.is_health_allowed_for_send
derive_operational_health_from_db = _target_health.derive_operational_health_from_db


def _row(**kwargs):
    base = {
        "username": None,
        "invite_link": None,
        "tg_id": None,
        "chat_type": "channel",
    }
    base.update(kwargs)
    return base


def test_needs_repair_tg_id_only_without_handle():
    h = classify_target(_row(tg_id=123456789))
    assert h["health"] == HEALTH_NEEDS_REPAIR


def test_invalid_peer_user_chat_type():
    h = classify_target(_row(chat_type="user", tg_id=111))
    assert h["health"] == HEALTH_INVALID


def test_invalid_private_message_link_invite():
    h = classify_target(
        _row(invite_link="https://t.me/c/2820391767/42", chat_type="supergroup")
    )
    assert h["health"] == HEALTH_INVALID


def test_invalid_message_permalink_in_username_field():
    h = classify_target(_row(username="https://t.me/mygroup/99999", chat_type="supergroup"))
    assert h["health"] == HEALTH_INVALID


def test_invalid_tme_c_anywhere_in_username():
    h = classify_target(_row(username="https://t.me/c/12345/1", chat_type="group"))
    assert h["health"] == HEALTH_INVALID


def test_joinable_username():
    h = classify_target(_row(username="mychannel", tg_id=None))
    assert h["health"] == HEALTH_JOINABLE


def test_joinable_invite_hash():
    h = classify_target(
        _row(invite_link="https://t.me/joinchat/AAAA", tg_id=None, username=None)
    )
    assert h["health"] == HEALTH_JOINABLE


def test_resolve_entity_prefers_username_over_wrong_tg_id():
    """Avoid sending with a cached numeric id when @username is present."""
    ent = resolve_executor_entity(
        _row(username="mychannel", tg_id=999001, chat_type="channel")
    )
    assert ent == "mychannel"


def test_resolve_entity_falls_back_to_tg_id():
    ent = resolve_executor_entity(_row(tg_id=-1001234567890, username=None, invite_link=None))
    assert ent == -1001234567890


def test_resolve_entity_uses_invite_when_no_username():
    ent = resolve_executor_entity(
        _row(username=None, invite_link="https://t.me/joinchat/ZZZZ", tg_id=111)
    )
    assert "joinchat" in ent


def test_user_chat_type_negative_id_is_channel_not_dm():
    """Rows mis-typed as user but with a channel id should still classify."""
    h = classify_target(
        _row(chat_type="user", tg_id=-1001234567890, username="cryptodiscussing")
    )
    assert h["health"] == HEALTH_JOINABLE


def test_username_with_zero_width_joinable():
    h = classify_target(_row(username="crypto\u200bdiscussing", tg_id=None, chat_type="channel"))
    assert h["health"] == HEALTH_JOINABLE


def test_parse_user_banned():
    r = parse_delivery_error_to_operational("UserBannedInChannel: User banned")
    assert r is not None and r[0] == HEALTH_BANNED


def test_parse_user_banned_false_positive_softens():
    msg = (
        "UserBannedInChannel: Telegram USER_BANNED_IN_CHANNEL — often a false positive right after join "
        "or during spam/slowmode windows."
    )
    r = parse_delivery_error_to_operational(msg, "UserBannedInChannel")
    assert r is not None and r[0] == HEALTH_NO_PERMISSION


def test_parse_slow_mode_code():
    r = parse_delivery_error_to_operational("SlowModeWait: wait", "SlowModeWait")
    assert r is not None and r[0] == HEALTH_NO_PERMISSION


def test_parse_peer_user():
    r = parse_delivery_error_to_operational("PeerUser(...) could not find the input entity")
    assert r is not None and r[0] == HEALTH_UNRESOLVED_ENTITY


def test_merge_operational_overrides_sendable():
    m = merge_intrinsic_and_operational(
        {"health": HEALTH_SENDABLE, "reason": "ok"},
        {"health": HEALTH_BANNED, "reason": "banned"},
    )
    assert m["health"] == HEALTH_BANNED


def test_binding_allows_pending_not_banned():
    assert is_health_allowed_for_binding("pending_approval") is True
    assert is_health_allowed_for_binding("banned") is False
    assert is_health_allowed_for_send("pending_approval") is False


def test_needs_repair_blocks_send_and_binding():
    assert is_health_allowed_for_send(HEALTH_NEEDS_REPAIR) is False
    assert is_health_allowed_for_binding(HEALTH_NEEDS_REPAIR) is False


def test_derive_operational_account_scoped_no_filter_after_limit():
    """
    Regression: SQLAlchemy forbids .filter() after .limit() on the same query.

    Account-scoped target health must apply account_id before order/limit so the
    endpoint GET /api/v1/targets?account_id=… does not 500.
    """

    class _StrictQuery:
        """Raises if filter() is chained after limit() (matches SQLAlchemy rules)."""

        def __init__(self) -> None:
            self._has_limit = False

        def filter(self, *args, **kwargs):
            if self._has_limit:
                raise RuntimeError("regression: Query.filter() after limit()")
            return self

        def order_by(self, *args):
            return self

        def limit(self, n):
            self._has_limit = True
            return self

        def first(self):
            return None

        def all(self):
            return []

    class _StrictSession:
        def query(self, model):
            return _StrictQuery()

    op, proven = derive_operational_health_from_db(_StrictSession(), 42, account_id=1)
    assert op is None
    assert proven is False
