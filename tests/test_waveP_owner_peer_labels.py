from src.messaging.peer_labels import (
    account_bucket,
    owner_safe_error_copy,
    owner_unavailable_label,
)


def test_owner_safe_error_copy_known():
    assert "rate limit" in owner_safe_error_copy("RATE_LIMITED").lower()
    assert "AI Draft" in owner_safe_error_copy("AI_DRAFT_PROVIDER_ERROR")
    assert owner_safe_error_copy("NO_WRITE_PERMISSION") == "Cannot post in this chat"


def test_account_bucket_and_labels():
    assert account_bucket(True, "OK") == "available"
    assert account_bucket(False, "AUTH_FAILED") == "attention"
    assert account_bucket(False, "PROTECTED") == "unavailable"
    assert owner_unavailable_label("RESERVED") == "Reserved"


def test_owner_safe_error_empty():
    assert owner_safe_error_copy(None, None) == ""
    assert owner_safe_error_copy("", "") == ""
