"""Unit tests for Telegram peer id marking (group/channel Send Now)."""
from src.messaging.peer_ids import marked_peer_id_from_entity, normalize_peer_target


def test_normalize_supergroup_bare_id_to_bot_api_marked():
    assert normalize_peer_target("4297144441", "supergroup") == "-1004297144441"


def test_normalize_channel_bare_id_to_bot_api_marked():
    assert normalize_peer_target("3569264265", "channel") == "-1003569264265"


def test_normalize_basic_group_bare_id():
    assert normalize_peer_target("12345", "group") == "-12345"


def test_normalize_keeps_already_marked():
    assert normalize_peer_target("-1004297144441", "supergroup") == "-1004297144441"
    assert normalize_peer_target("-12345", "group") == "-12345"


def test_normalize_private_unchanged():
    assert normalize_peer_target("8531893204", "private") == "8531893204"
    assert normalize_peer_target("@someone", "private") == "@someone"


def test_marked_peer_id_from_entity():
    assert marked_peer_id_from_entity(4297144441, "supergroup") == "-1004297144441"
    assert marked_peer_id_from_entity(None, "supergroup") is None
