"""
Production-ready TDATA zip import pipeline.

- Safe zip extraction (zip-slip prevention, size/file limits).
- Single extraction pass; each inner zip gets a unique temp directory.
- Multi-candidate discovery (all tdata + session strings), then per-account import with robust error handling.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Limits for safe extraction (prevent zip bombs and runaway disk use)
MAX_ZIP_FILE_SIZE_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_TOTAL_BYTES = 120 * 1024 * 1024
MAX_EXTRACTED_FILE_COUNT = 10_000
MAX_INNER_ZIPS = 200


def _is_safe_path(member_path: str, dest_dir: Path) -> bool:
    """Return True if extracting member_path into dest_dir would not escape dest_dir (zip-slip safe)."""
    dest_dir = dest_dir.resolve()
    if os.path.isabs(member_path):
        return False
    # Normalize and remove any '..' or leading slashes
    parts = Path(member_path).parts
    if ".." in parts or member_path.startswith("/"):
        return False
    resolved = (dest_dir / member_path).resolve()
    try:
        resolved.relative_to(dest_dir)
    except ValueError:
        return False
    return True


def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    max_total_bytes: int = MAX_EXTRACTED_TOTAL_BYTES,
    max_file_count: int = MAX_EXTRACTED_FILE_COUNT,
) -> Tuple[int, int, Optional[str]]:
    """
    Extract zip into dest_dir with zip-slip protection and size/count limits.
    Returns (bytes_extracted, files_extracted, error_message or None).
    """
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    file_count = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if file_count >= max_file_count:
                    return total_bytes, file_count, f"Too many files (limit {max_file_count})"
                if total_bytes >= max_total_bytes:
                    return total_bytes, file_count, f"Extracted size limit exceeded ({max_total_bytes})"
                name = info.filename.rstrip("/")
                if not name:
                    continue
                name = name.replace("\\", "/").strip()
                if not name:
                    continue
                if ".." in name.split("/") or name.startswith("/"):
                    logger.warning("tdata_import: rejected zip-slip path %s", name)
                    continue
                if not _is_safe_path(name, dest_dir):
                    logger.warning("tdata_import: rejected unsafe path %s", name)
                    continue
                target = dest_dir / name
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                file_count += 1
                # Approximate size from compress_type
                total_bytes += info.file_size or 0
                if total_bytes > max_total_bytes:
                    return total_bytes, file_count, f"Extracted size limit exceeded ({max_total_bytes})"
                zf.extract(info, dest_dir)
        return total_bytes, file_count, None
    except zipfile.BadZipFile as e:
        return total_bytes, file_count, f"Invalid zip: {e}"
    except Exception as e:
        return total_bytes, file_count, str(e)


def discover_candidates(
    root_dir: Path,
    passcode: Optional[str],
    tdata_to_session_fn: Callable[[str, Optional[str]], Any],
    find_tdata_root_fn: Callable[[Path], Path],
    find_all_sessions_fn: Callable[[Path], List[str]],
) -> Tuple[List[dict], dict]:
    """
    Discover all session-string candidates from an extracted root (single or multi-account layout).

    - Inner zips: extracted to account_0, account_1, ...; each is a base.
    - No inner zips: every top-level directory in the extracted root is an account base (even if only one).
      Example: root/14237076181/, root/14427886105/ => bases 14237076181 and 14427886105.
      Non-dir root entries (e.g. upload.zip) are ignored.
    - For each base, tdata is detected by directory name (case-insensitive): base/tdata, base/TDATA, etc.
      map.json is NOT required for detection. Prefer direct child base/tdata first.
    - If tdata is found but conversion fails, we record it in debug["failed_tdata"] so the API never
      reports "No tdata folder" when tdata was present.
    - Restart app (e.g. systemd/gunicorn) after code changes so new discovery runs.
    """
    root_dir = Path(root_dir).resolve()
    debug: dict = {}
    if not root_dir.is_dir():
        return [], debug

    # 1) Enumerate inner zips (excluding the main upload zip if present)
    zip_files = sorted(root_dir.rglob("*.zip"))
    main_zip = root_dir / "upload.zip"
    inner_zips = [z for z in zip_files if z.is_file() and not (z == main_zip)]

    bases: List[Tuple[Path, str]] = [(root_dir, "root")]

    if inner_zips:
        if len(inner_zips) > MAX_INNER_ZIPS:
            logger.warning("tdata_import: limiting inner zips from %d to %d", len(inner_zips), MAX_INNER_ZIPS)
            inner_zips = inner_zips[:MAX_INNER_ZIPS]
        for idx, zpath in enumerate(inner_zips):
            unique_sub = root_dir / f"account_{idx}"
            if unique_sub.exists():
                shutil.rmtree(unique_sub, ignore_errors=True)
            unique_sub.mkdir(parents=True, exist_ok=True)
            _, _, err = safe_extract_zip(
                zpath,
                unique_sub,
                max_total_bytes=MAX_EXTRACTED_TOTAL_BYTES // max(len(inner_zips), 1),
                max_file_count=MAX_EXTRACTED_FILE_COUNT // max(len(inner_zips), 1),
            )
            if err:
                logger.warning("tdata_import: extract %s failed: %s", zpath.name, err)
                continue
            bases.append((unique_sub, f"account_{idx}"))
    else:
        # No inner zips: treat every top-level directory as an account base (single or multi-account).
        top_dirs = sorted(p for p in root_dir.iterdir() if p.is_dir() and p.name != "upload.zip")
        if top_dirs:
            bases = [(p.resolve(), p.name) for p in top_dirs]
            debug["top_level_folders"] = [p.name for p in top_dirs]
            logger.info("tdata_import: %d top-level folder(s) as account bases: %s", len(top_dirs), [p.name for p in top_dirs])

    # 2) From each base: detect tdata by name (case-insensitive), convert, and collect session strings
    seen_strings: set = set()
    candidates: List[dict] = []
    failed_tdata: List[dict] = []  # tdata found but conversion failed
    index = 0

    def _find_tdata_in_base(base: Path) -> Optional[Path]:
        """Return path to tdata folder under base. Prefer direct child base/tdata; accept any case. No map.json required."""
        base = Path(base).resolve()
        if not base.is_dir():
            return None
        # Prefer direct child base/tdata (then case-insensitive direct, then any descendant)
        direct = base / "tdata"
        if direct.is_dir():
            return direct
        for p in base.iterdir():
            if p.is_dir() and p.name.lower() == "tdata":
                return p
        for p in base.rglob("*"):
            if p.is_dir() and p.name.lower() == "tdata":
                return p
        return None

    for base_path, source in bases:
        base_path = Path(base_path).resolve()
        tdata_root = _find_tdata_in_base(base_path)
        logger.info("tdata_import: base=%s found_tdata=%s", base_path, tdata_root if tdata_root is not None else "None")

        if tdata_root is None:
            try:
                tdata_root = find_tdata_root_fn(base_path)
            except ValueError:
                pass
            except Exception as e:
                logger.warning("tdata_import: %s find_tdata_root failed: %s", source, e)

        if tdata_root is not None:
            try:
                s = tdata_to_session_fn(str(tdata_root), passcode)
                s = (s or "").strip()
                if s and len(s) >= 90 and s not in seen_strings:
                    seen_strings.add(s)
                    index += 1
                    candidates.append({"session_string": s, "source": source, "index": index})
                    logger.info("tdata_import: %s tdata -> 1 session", source)
            except Exception as e:
                err_msg = str(e)
                failed_tdata.append({"source": source, "tdata_path": str(tdata_root), "error": err_msg})
                logger.warning("tdata_import: %s tdata conversion failed: %s", source, err_msg)

        for s in find_all_sessions_fn(base_path):
            s = (s or "").strip()
            if s and len(s) >= 90 and s not in seen_strings:
                seen_strings.add(s)
                index += 1
                candidates.append({"session_string": s, "source": source, "index": index})

    # When no candidates but we had top-level folders, list first folder contents for debugging
    if not candidates and debug.get("top_level_folders"):
        try:
            first_sub = root_dir / debug["top_level_folders"][0]
            if first_sub.is_dir():
                names = sorted(p.name for p in first_sub.iterdir())
                debug["first_folder_contents"] = names[:40]
        except Exception:
            pass

    if failed_tdata:
        debug["failed_tdata"] = failed_tdata

    return candidates, debug
