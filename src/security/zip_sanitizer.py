"""
SECURITY HOTFIX: Safe ZIP extraction with path validation
Fixes CRITICAL vulnerability: ZIP Path Traversal (CWE-22)
"""
import os
import zipfile
from pathlib import Path
from typing import List
import structlog

logger = structlog.get_logger(__name__)


class ZipSecurity:
    """Secure ZIP file handling with path validation"""

    # Dangerous file patterns that should never be extracted
    DANGEROUS_PATTERNS = [
        '/etc/', '/root/', '/.ssh/', '/.bashrc', '/.profile',
        '/var/log/', '/usr/bin/', '/bin/', '/sbin/',
        '.env', 'credentials', 'secrets', 'password',
        'config.py', 'settings.py', '.git/',
    ]

    @staticmethod
    def is_safe_member(member: str, extract_dir: Path) -> bool:
        """
        Validate ZIP member path is safe for extraction

        Security checks:
        1. No absolute paths
        2. No parent directory traversal (..)
        3. No dangerous file patterns
        4. Path must resolve within extract directory

        Returns: True if safe, False if path traversal attempt
        """
        try:
            # Normalize path separators
            member_normalized = member.replace('\\', '/')

            # 1. Check for absolute paths
            if member_normalized.startswith('/') or (len(member_normalized) > 1 and member_normalized[1] == ':'):
                logger.warning("ZIP security: Absolute path rejected", member=member)
                return False

            # 2. Check for path traversal patterns
            normalized = os.path.normpath(member_normalized)
            if normalized.startswith('..') or '/..' in normalized or '\\..' in normalized:
                logger.warning("ZIP security: Path traversal rejected", member=member)
                return False

            # Check each path component
            parts = normalized.replace('\\', '/').split('/')
            for part in parts:
                if part == '..':
                    logger.warning("ZIP security: Parent directory rejected", member=member)
                    return False

            # 3. Check for dangerous file patterns
            member_lower = member_normalized.lower()
            for pattern in ZipSecurity.DANGEROUS_PATTERNS:
                if pattern in member_lower:
                    logger.warning("ZIP security: Dangerous pattern rejected",
                                 member=member, pattern=pattern)
                    return False

            # 4. Verify resolved path is within extraction directory
            target_path = extract_dir / normalized
            try:
                resolved = target_path.resolve()
                extract_resolved = extract_dir.resolve()

                # Ensure the resolved path starts with the extraction directory
                if not str(resolved).startswith(str(extract_resolved)):
                    logger.warning("ZIP security: Path escape rejected",
                                 member=member, resolved=str(resolved))
                    return False
            except (OSError, ValueError) as e:
                logger.warning("ZIP security: Path resolution failed",
                             member=member, error=str(e))
                return False

            return True

        except Exception as e:
            logger.error("ZIP security: Validation error", member=member, error=str(e))
            return False

    @staticmethod
    def safe_extract(zip_path: Path, extract_dir: Path) -> List[str]:
        """
        Extract ZIP file safely with path validation

        Security measures:
        - Validates all paths before extraction
        - Rejects any path traversal attempts
        - Cleans up on failure

        Args:
            zip_path: Path to ZIP file
            extract_dir: Directory to extract to

        Returns:
            List of extracted file paths

        Raises:
            ValueError: If ZIP contains unsafe paths or is invalid
        """
        extracted = []
        extract_dir = Path(extract_dir).resolve()

        # Create extraction directory if it doesn't exist
        extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # SECURITY: Validate ALL members BEFORE extracting ANY
                unsafe_members = []
                for member in zip_ref.namelist():
                    if not ZipSecurity.is_safe_member(member, extract_dir):
                        unsafe_members.append(member)

                if unsafe_members:
                    logger.error("ZIP security: Rejected unsafe ZIP file",
                               unsafe_count=len(unsafe_members),
                               samples=unsafe_members[:5])
                    raise ValueError(
                        f"ZIP file rejected: Contains {len(unsafe_members)} unsafe paths. "
                        f"Examples: {unsafe_members[:3]}"
                    )

                # All paths validated - now extract safely
                for member in zip_ref.namelist():
                    # Skip directories (they'll be created automatically)
                    if member.endswith('/'):
                        continue

                    # Double-check safety (defense in depth)
                    if ZipSecurity.is_safe_member(member, extract_dir):
                        zip_ref.extract(member, extract_dir)
                        extracted.append(member)
                        logger.debug("ZIP extracted file", file=member)

                logger.info("ZIP extraction complete",
                          total_files=len(extracted),
                          extract_dir=str(extract_dir))
                return extracted

        except zipfile.BadZipFile:
            raise ValueError("Invalid or corrupted ZIP file")
        except Exception as e:
            # Clean up partially extracted files on error
            for file in extracted:
                try:
                    file_path = extract_dir / file
                    if file_path.exists():
                        file_path.unlink()
                except Exception:
                    pass

            if isinstance(e, ValueError):
                raise
            raise ValueError(f"ZIP extraction failed: {str(e)}")

    @staticmethod
    def get_safe_members(zip_path: Path) -> List[str]:
        """
        Get list of safe members in a ZIP file without extracting

        Useful for previewing ZIP contents before extraction
        """
        safe_members = []

        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for member in zip_ref.namelist():
                    # Use a dummy extract dir for validation
                    dummy_dir = Path('/tmp/validation')
                    if ZipSecurity.is_safe_member(member, dummy_dir):
                        safe_members.append(member)
        except zipfile.BadZipFile:
            pass

        return safe_members
