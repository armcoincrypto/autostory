"""Login brute-force protection and media upload content checks (Wave B)."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from typing import Optional

# Conservative temporary rate limit — never permanent account lockout.
LOGIN_WINDOW_SEC = 15 * 60
LOGIN_MAX_FAILURES_PER_IDENTITY = 8
LOGIN_MAX_FAILURES_PER_IP = 25

_login_lock = Lock()
_login_failures_by_identity: dict[str, deque[float]] = defaultdict(deque)
_login_failures_by_ip: dict[str, deque[float]] = defaultdict(deque)

MEDIA_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB
ALLOWED_MEDIA_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"})

# Magic-byte → (extension set, media kind)
_MAGIC_RULES: tuple[tuple[bytes, frozenset[str], str], ...] = (
    (b"\xff\xd8\xff", frozenset({".jpg", ".jpeg"}), "photo"),
    (b"\x89PNG\r\n\x1a\n", frozenset({".png"}), "photo"),
    (b"RIFF", frozenset({".webp"}), "photo"),  # WebP: RIFF....WEBP
    (b"\x00\x00\x00", frozenset({".mp4", ".mov"}), "video"),  # ftyp often at offset 4
)


def _prune(dq: deque[float], now: float, window: float) -> None:
    while dq and now - dq[0] >= window:
        dq.popleft()


def login_rate_limited(*, ip: str, username: str) -> tuple[bool, int]:
    """Return (blocked, retry_after_sec)."""
    now = time.monotonic()
    ip_key = (ip or "unknown").strip() or "unknown"
    id_key = f"{ip_key}|{(username or '').strip().lower()}"
    with _login_lock:
        ip_q = _login_failures_by_ip[ip_key]
        id_q = _login_failures_by_identity[id_key]
        _prune(ip_q, now, LOGIN_WINDOW_SEC)
        _prune(id_q, now, LOGIN_WINDOW_SEC)
        if len(id_q) >= LOGIN_MAX_FAILURES_PER_IDENTITY:
            retry = int(max(1, LOGIN_WINDOW_SEC - (now - id_q[0])))
            return True, retry
        if len(ip_q) >= LOGIN_MAX_FAILURES_PER_IP:
            retry = int(max(1, LOGIN_WINDOW_SEC - (now - ip_q[0])))
            return True, retry
        return False, 0


def record_login_failure(*, ip: str, username: str) -> None:
    now = time.monotonic()
    ip_key = (ip or "unknown").strip() or "unknown"
    id_key = f"{ip_key}|{(username or '').strip().lower()}"
    with _login_lock:
        ip_q = _login_failures_by_ip[ip_key]
        id_q = _login_failures_by_identity[id_key]
        _prune(ip_q, now, LOGIN_WINDOW_SEC)
        _prune(id_q, now, LOGIN_WINDOW_SEC)
        ip_q.append(now)
        id_q.append(now)


def clear_login_failures(*, ip: str, username: str) -> None:
    ip_key = (ip or "unknown").strip() or "unknown"
    id_key = f"{ip_key}|{(username or '').strip().lower()}"
    with _login_lock:
        _login_failures_by_identity.pop(id_key, None)
        # Do not wipe entire IP on one success — identity key is enough.


def sniff_media_kind(path: Path, declared_ext: str) -> tuple[bool, str, str]:
    """Validate file magic against declared extension. Returns (ok, kind, error)."""
    ext = (declared_ext or "").lower()
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        return False, "", f"Type {ext} not allowed. Use: jpg, png, webp, mp4, mov"
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
    except OSError:
        return False, "", "Unable to read uploaded file."
    if len(head) < 12:
        return False, "", "File too small or empty."

    if ext in {".jpg", ".jpeg"}:
        if head.startswith(b"\xff\xd8\xff"):
            return True, "photo", ""
        return False, "", "File content is not a valid JPEG."
    if ext == ".png":
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return True, "photo", ""
        return False, "", "File content is not a valid PNG."
    if ext == ".webp":
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return True, "photo", ""
        return False, "", "File content is not a valid WebP."
    if ext in {".mp4", ".mov"}:
        # ISO BMFF: size(4) + 'ftyp' at offset 4
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return True, "video", ""
        return False, "", "File content is not a valid MP4/MOV."
    return False, "", "Unsupported media type."
