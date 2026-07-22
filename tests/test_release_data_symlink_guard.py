"""Regression: clean releases must bind data/ to shared /opt/autostory/data.

Production failure 2026-07-22: a mispackaged release shipped a local empty
``data/storyfleet.db``. Relative ``DATABASE_URL=sqlite:///./data/storyfleet.db``
then caused Admin Dashboard ``Invalid username or password`` (USER_NOT_FOUND /
WRONG_DATABASE) despite valid credentials existing in the shared DB.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "release" / "build_immutable_release.sh"


def test_build_immutable_release_script_enforces_shared_data_symlink():
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'ln -sfn /opt/autostory/data "$OUT/data"' in text
    assert "release data must be symlink" in text
    assert "release data symlink target mismatch" in text
    assert "storyfleet.db away from shared DB" in text


def test_assert_release_data_link_accepts_shared_symlink(tmp_path: Path):
    from scripts.release.release_data_link import assert_release_data_link

    data = tmp_path / "data"
    data.symlink_to("/opt/autostory/data")
    assert_release_data_link(tmp_path)


def test_assert_release_data_link_rejects_local_sqlite(tmp_path: Path):
    from scripts.release.release_data_link import ReleaseDataLinkError, assert_release_data_link

    data = tmp_path / "data"
    data.mkdir()
    (data / "storyfleet.db").write_bytes(b"not-a-shared-db")
    with pytest.raises(ReleaseDataLinkError):
        assert_release_data_link(tmp_path)
