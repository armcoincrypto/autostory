"""
Utility Helper Functions
"""
import re
import os
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Tuple
import structlog

logger = structlog.get_logger(__name__)


def format_phone(phone: str) -> str:
    """
    Format phone number to standard format

    Args:
        phone: Raw phone number input

    Returns:
        Formatted phone number with + prefix
    """
    # Remove all non-digit characters except +
    cleaned = re.sub(r'[^\d+]', '', phone)

    # Ensure it starts with +
    if not cleaned.startswith('+'):
        cleaned = '+' + cleaned

    return cleaned


def validate_media(file_path: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a media file for story publishing

    Args:
        file_path: Path to media file

    Returns:
        Tuple of (is_valid, error_message)
    """
    path = Path(file_path)

    # Check existence
    if not path.exists():
        return False, f"File not found: {file_path}"

    # Check extension
    valid_extensions = {
        'photo': ['.jpg', '.jpeg', '.png', '.webp'],
        'video': ['.mp4', '.mov', '.avi', '.webm']
    }

    ext = path.suffix.lower()
    all_valid = valid_extensions['photo'] + valid_extensions['video']

    if ext not in all_valid:
        return False, f"Unsupported file type: {ext}"

    # Check file size (max 50MB)
    max_size = 50 * 1024 * 1024  # 50MB
    file_size = path.stat().st_size

    if file_size > max_size:
        return False, f"File too large: {file_size / 1024 / 1024:.1f}MB (max 50MB)"

    if file_size == 0:
        return False, "File is empty"

    return True, None


def utc_now() -> datetime:
    """Timezone-aware UTC now. Replaces deprecated datetime.utcnow(). Returns naive UTC for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_from_timestamp(ts: float) -> datetime:
    """Timezone-aware UTC from timestamp. Replaces deprecated datetime.utcfromtimestamp()."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def generate_session_name(phone: str) -> str:
    """
    Generate a unique session name from phone number

    Args:
        phone: Phone number

    Returns:
        Hashed session name
    """
    hash_input = f"{phone}_{utc_now().timestamp()}"
    return hashlib.md5(hash_input.encode()).hexdigest()[:12]


def chunk_list(lst: List, chunk_size: int) -> List[List]:
    """
    Split a list into chunks

    Args:
        lst: List to split
        chunk_size: Size of each chunk

    Returns:
        List of chunks
    """
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


def safe_filename(filename: str) -> str:
    """
    Create a safe filename by removing/replacing invalid characters

    Args:
        filename: Original filename

    Returns:
        Safe filename
    """
    # Remove or replace invalid characters
    safe = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Remove leading/trailing spaces and dots
    safe = safe.strip('. ')
    # Ensure it's not empty
    if not safe:
        safe = 'unnamed'
    return safe


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """
    Truncate text to a maximum length

    Args:
        text: Text to truncate
        max_length: Maximum length
        suffix: Suffix to add if truncated

    Returns:
        Truncated text
    """
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


def parse_user_mentions(text: str) -> List[str]:
    """
    Extract @username mentions from text

    Args:
        text: Text containing mentions

    Returns:
        List of usernames (without @)
    """
    pattern = r'@([a-zA-Z0-9_]{5,32})'
    return re.findall(pattern, text)


def format_number(num: int) -> str:
    """
    Format large numbers with K/M suffix

    Args:
        num: Number to format

    Returns:
        Formatted string
    """
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)


def time_ago(dt: datetime) -> str:
    """
    Get human-readable time difference

    Args:
        dt: Datetime to compare

    Returns:
        Human-readable string
    """
    now = utc_now()
    diff = now - dt

    seconds = diff.total_seconds()

    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes}m ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours}h ago"
    else:
        days = int(seconds / 86400)
        return f"{days}d ago"
