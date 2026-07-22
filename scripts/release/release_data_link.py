"""Helpers to validate immutable release data binding (no Telegram, no secrets)."""
from __future__ import annotations

from pathlib import Path

SHARED_DATA = Path("/opt/autostory/data")
SHARED_DB = SHARED_DATA / "storyfleet.db"


class ReleaseDataLinkError(RuntimeError):
    """Release data/ is not bound to the shared production data directory."""


def assert_release_data_link(release_root: Path | str) -> Path:
    """Require ``release/data -> /opt/autostory/data`` (not a local SQLite tree)."""
    root = Path(release_root)
    data = root / "data"
    if not data.is_symlink():
        raise ReleaseDataLinkError(f"release data must be a symlink: {data}")
    target = data.resolve()
    if target != SHARED_DATA.resolve():
        raise ReleaseDataLinkError(
            f"release data symlink target mismatch: {target} (expected {SHARED_DATA})"
        )
    db = data / "storyfleet.db"
    if db.exists():
        db_real = db.resolve()
        if db_real != SHARED_DB.resolve():
            raise ReleaseDataLinkError(
                f"storyfleet.db resolves away from shared DB: {db_real}"
            )
    return target
