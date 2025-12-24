"""
Tests for Utility Functions
"""
import pytest
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import os

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.helpers import (
    format_phone,
    validate_media,
    generate_session_name,
    chunk_list,
    safe_filename,
    truncate_text,
    parse_user_mentions,
    format_number,
    time_ago,
)


class TestFormatPhone:
    """Tests for format_phone function"""

    def test_already_formatted(self):
        """Test already formatted number"""
        assert format_phone("+1234567890") == "+1234567890"

    def test_without_plus(self):
        """Test number without plus"""
        assert format_phone("1234567890") == "+1234567890"

    def test_with_spaces(self):
        """Test number with spaces"""
        assert format_phone("+1 234 567 890") == "+1234567890"

    def test_with_dashes(self):
        """Test number with dashes"""
        assert format_phone("+1-234-567-890") == "+1234567890"

    def test_with_parentheses(self):
        """Test number with parentheses"""
        assert format_phone("+1 (234) 567-890") == "+1234567890"


class TestValidateMedia:
    """Tests for validate_media function"""

    def test_nonexistent_file(self):
        """Test nonexistent file"""
        is_valid, error = validate_media("/nonexistent/file.jpg")
        assert is_valid == False
        assert "not found" in error.lower()

    def test_valid_image(self):
        """Test valid image file"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake image data")
            temp_path = f.name

        try:
            is_valid, error = validate_media(temp_path)
            assert is_valid == True
            assert error is None
        finally:
            os.unlink(temp_path)

    def test_unsupported_extension(self):
        """Test unsupported file extension"""
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            f.write(b"some data")
            temp_path = f.name

        try:
            is_valid, error = validate_media(temp_path)
            assert is_valid == False
            assert "unsupported" in error.lower()
        finally:
            os.unlink(temp_path)

    def test_empty_file(self):
        """Test empty file"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            temp_path = f.name

        try:
            is_valid, error = validate_media(temp_path)
            assert is_valid == False
            assert "empty" in error.lower()
        finally:
            os.unlink(temp_path)


class TestGenerateSessionName:
    """Tests for generate_session_name function"""

    def test_generates_unique_names(self):
        """Test that names are unique"""
        name1 = generate_session_name("+1234567890")
        name2 = generate_session_name("+1234567890")

        # Names should be different due to timestamp
        assert name1 != name2

    def test_correct_length(self):
        """Test name length"""
        name = generate_session_name("+1234567890")
        assert len(name) == 12


class TestChunkList:
    """Tests for chunk_list function"""

    def test_even_chunks(self):
        """Test even chunks"""
        result = chunk_list([1, 2, 3, 4, 5, 6], 2)
        assert result == [[1, 2], [3, 4], [5, 6]]

    def test_uneven_chunks(self):
        """Test uneven chunks"""
        result = chunk_list([1, 2, 3, 4, 5], 2)
        assert result == [[1, 2], [3, 4], [5]]

    def test_empty_list(self):
        """Test empty list"""
        result = chunk_list([], 2)
        assert result == []


class TestSafeFilename:
    """Tests for safe_filename function"""

    def test_removes_invalid_chars(self):
        """Test removal of invalid characters"""
        result = safe_filename('file<>:"/\\|?*.txt')
        assert '<' not in result
        assert '>' not in result
        assert ':' not in result

    def test_strips_dots_and_spaces(self):
        """Test stripping of dots and spaces"""
        result = safe_filename("  .file.  ")
        assert not result.startswith(' ')
        assert not result.endswith(' ')
        assert not result.startswith('.')
        assert not result.endswith('.')


class TestTruncateText:
    """Tests for truncate_text function"""

    def test_no_truncation_needed(self):
        """Test short text"""
        result = truncate_text("short", 10)
        assert result == "short"

    def test_truncation(self):
        """Test truncation"""
        result = truncate_text("this is a long text", 10)
        assert len(result) == 10
        assert result.endswith("...")


class TestParseUserMentions:
    """Tests for parse_user_mentions function"""

    def test_single_mention(self):
        """Test single mention"""
        result = parse_user_mentions("Hello @username")
        assert result == ["username"]

    def test_multiple_mentions(self):
        """Test multiple mentions"""
        result = parse_user_mentions("@user1 and @user2")
        assert result == ["user1", "user2"]

    def test_no_mentions(self):
        """Test no mentions"""
        result = parse_user_mentions("Hello world")
        assert result == []


class TestFormatNumber:
    """Tests for format_number function"""

    def test_small_number(self):
        """Test small number"""
        assert format_number(500) == "500"

    def test_thousands(self):
        """Test thousands"""
        assert format_number(1500) == "1.5K"

    def test_millions(self):
        """Test millions"""
        assert format_number(1500000) == "1.5M"


class TestTimeAgo:
    """Tests for time_ago function"""

    def test_just_now(self):
        """Test just now"""
        result = time_ago(datetime.utcnow())
        assert result == "just now"

    def test_minutes_ago(self):
        """Test minutes ago"""
        past = datetime.utcnow() - timedelta(minutes=5)
        result = time_ago(past)
        assert "m ago" in result

    def test_hours_ago(self):
        """Test hours ago"""
        past = datetime.utcnow() - timedelta(hours=3)
        result = time_ago(past)
        assert "h ago" in result

    def test_days_ago(self):
        """Test days ago"""
        past = datetime.utcnow() - timedelta(days=2)
        result = time_ago(past)
        assert "d ago" in result
