"""Slow mode owner copy — no auto-retry."""
from src.messaging.errors import map_dm_error
from src.messaging.peer_labels import owner_safe_error_copy


class SlowModeWaitError(Exception):
    def __init__(self, seconds=None):
        self.seconds = seconds
        super().__init__("SlowModeWaitError")


def test_slow_mode_includes_wait_seconds_and_no_class_name():
    code, msg, extras = map_dm_error(SlowModeWaitError(42))
    assert code == "SLOW_MODE"
    assert "42" in msg
    assert "slow mode" in msg.lower()
    assert "SlowModeWaitError" not in msg
    assert extras.get("retry_after") == 42
    assert extras.get("auto_retry") is False


def test_slow_mode_without_seconds():
    code, msg, extras = map_dm_error(SlowModeWaitError(None))
    assert code == "SLOW_MODE"
    assert "later" in msg.lower()
    assert "SlowMode" not in msg
    assert extras.get("auto_retry") is False
    assert "retry_after" not in extras


def test_owner_safe_copy_hides_exception_name():
    copy = owner_safe_error_copy("SLOW_MODE", "SlowModeWaitError: wait")
    assert "slow mode" in copy.lower()
    assert "SlowModeWaitError" not in copy


def test_name_only_slow_mode_normalizes():
    class SlowModeWaitError(Exception):
        pass

    code, msg, extras = map_dm_error(SlowModeWaitError("wait"))
    assert code == "SLOW_MODE"
    assert "SlowModeWaitError" not in msg
    assert extras.get("auto_retry") is False
