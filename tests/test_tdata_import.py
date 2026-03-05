"""
Tests for TDATA zip discovery pipeline.

Covers layout: root/14237076181/tdata/ and root/14237076181/2fa.txt
(single-account and multi-account top-level folders).
"""
import pytest
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.tdata_import import discover_candidates

# Fake session string (version 1 + base64, length >= 90)
FAKE_SESSION = "1" + "A" * 99


def _mock_tdata_to_session(path: str, pwd=None):
    """Mock converter: return fake session string for any tdata path."""
    return FAKE_SESSION


def _find_tdata_root_raises(base: Path):
    """Mock that always raises (so discovery relies on _find_tdata_in_base)."""
    raise ValueError("No tdata")


def _find_all_sessions_empty(base: Path):
    """Mock: no session strings from files."""
    return []


class TestDiscoverCandidatesSingleAccount:
    """Discovery with one top-level folder containing tdata/ and 2fa.txt."""

    def test_finds_tdata_under_account_folder(self):
        """Root contains 14237076181/ with tdata/ and 2fa.txt; discovery should find one candidate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = root / "14237076181"
            account.mkdir()
            (account / "tdata").mkdir()
            (account / "2fa.txt").write_text("2fa")
            candidates, debug = discover_candidates(
                root,
                passcode=None,
                tdata_to_session_fn=_mock_tdata_to_session,
                find_tdata_root_fn=_find_tdata_root_raises,
                find_all_sessions_fn=_find_all_sessions_empty,
            )
            assert len(candidates) == 1
            assert candidates[0]["source"] == "14237076181"
            assert candidates[0]["session_string"] == FAKE_SESSION
            assert debug.get("top_level_folders") == ["14237076181"]

    def test_finds_tdata_case_insensitive(self):
        """TDATA (uppercase) under account folder should be found."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = root / "14427886105"
            account.mkdir()
            (account / "TDATA").mkdir()
            (account / "2fa.txt").write_text("x")
            candidates, debug = discover_candidates(
                root,
                passcode=None,
                tdata_to_session_fn=_mock_tdata_to_session,
                find_tdata_root_fn=_find_tdata_root_raises,
                find_all_sessions_fn=_find_all_sessions_empty,
            )
            assert len(candidates) == 1
            assert candidates[0]["source"] == "14427886105"


class TestDiscoverCandidatesMultiAccount:
    """Discovery with multiple top-level folders."""

    def test_multiple_top_level_folders_each_with_tdata(self):
        """Multiple account folders; each has tdata/; all become candidates."""
        def unique_session(path: str, pwd=None):
            # Unique per path so dedup doesn't collapse; length >= 90
            return "1" + "B" * 80 + str(abs(hash(path)) % 10**9).zfill(9)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ["14237076181", "14427886105", "14244611976"]:
                account = root / name
                account.mkdir()
                (account / "tdata").mkdir()
            candidates, debug = discover_candidates(
                root,
                passcode=None,
                tdata_to_session_fn=unique_session,
                find_tdata_root_fn=_find_tdata_root_raises,
                find_all_sessions_fn=_find_all_sessions_empty,
            )
            assert len(candidates) == 3
            sources = {c["source"] for c in candidates}
            assert sources == {"14237076181", "14427886105", "14244611976"}
            assert debug.get("top_level_folders") == ["14237076181", "14244611976", "14427886105"]

    def test_tdata_found_but_conversion_failed_recorded(self):
        """When conversion raises, failed_tdata is populated and we do not report 'No tdata folder'."""
        def failing_converter(path: str, pwd=None):
            raise RuntimeError("opentele key file missing")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = root / "14237076181"
            account.mkdir()
            (account / "tdata").mkdir()
            (account / "2fa.txt").write_text("x")
            candidates, debug = discover_candidates(
                root,
                passcode=None,
                tdata_to_session_fn=failing_converter,
                find_tdata_root_fn=_find_tdata_root_raises,
                find_all_sessions_fn=_find_all_sessions_empty,
            )
            assert len(candidates) == 0
            failed = debug.get("failed_tdata")
            assert failed is not None
            assert len(failed) == 1
            assert failed[0]["source"] == "14237076181"
            assert "tdata" in failed[0]["tdata_path"].replace("\\", "/")
            assert "opentele" in failed[0]["error"] or "key" in failed[0]["error"].lower()
