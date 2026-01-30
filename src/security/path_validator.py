"""
SECURITY HOTFIX: Secure path validation and sanitization
Fixes HIGH vulnerability: Directory Traversal (CWE-22)
"""
import os
import re
import time
from pathlib import Path
from typing import Tuple, Optional
import structlog

logger = structlog.get_logger(__name__)


class PathValidator:
    """Secure path validation to prevent directory traversal attacks"""

    # Allowed root directories for tdata imports
    ALLOWED_ROOTS = [
        '/opt/autostory/data',
        '/opt/autostory/uploads',
        '/tmp/autostory',
        '/var/tmp/autostory',
    ]

    # Maximum path depth to prevent deep traversal
    MAX_DEPTH = 15

    # Dangerous path patterns
    DANGEROUS_PATTERNS = [
        '/etc/', '/root/', '/.ssh/', '/var/log/',
        '/usr/bin/', '/bin/', '/sbin/', '/boot/',
        '/proc/', '/sys/', '/dev/',
        '.env', 'credentials', 'secrets', 'password',
        'shadow', 'passwd', 'sudoers',
    ]

    @staticmethod
    def sanitize_path(user_input: str) -> Tuple[bool, Optional[Path], str]:
        """
        Sanitize and validate user-provided path

        Security checks:
        1. Input type validation
        2. Dangerous character removal
        3. Path traversal detection
        4. Depth limit check
        5. Dangerous pattern detection
        6. Allowed roots verification

        Args:
            user_input: Raw user-provided path string

        Returns:
            Tuple of (is_valid, sanitized_path, message)
        """
        # 1. Input validation
        if not user_input or not isinstance(user_input, str):
            return False, None, "Invalid input: path must be a non-empty string"

        # 2. Remove dangerous shell characters
        dangerous_chars = [';', '&', '|', '`', '$', '\n', '\r', '\t', '>', '<', '(', ')', '{', '}']
        cleaned = user_input.strip()
        for char in dangerous_chars:
            if char in cleaned:
                logger.warning("Path security: Dangerous character removed",
                             char=repr(char), input=user_input[:50])
                cleaned = cleaned.replace(char, '')

        cleaned = cleaned.strip()
        if not cleaned:
            return False, None, "Invalid input: path is empty after sanitization"

        try:
            # Convert to Path object
            path = Path(cleaned)

            # Make absolute if relative
            if not path.is_absolute():
                path = Path.cwd() / path

            # 3. Check for path traversal before resolving
            path_str = str(path)
            if '..' in path_str:
                # Count traversals
                traversal_count = path_str.count('..')
                if traversal_count > 3:
                    logger.warning("Path security: Excessive traversal rejected",
                                 count=traversal_count, path=path_str[:100])
                    return False, None, f"Path rejected: excessive parent traversal ({traversal_count} levels)"

            # Resolve to absolute path (follows symlinks)
            try:
                real_path = path.resolve()
            except (OSError, RuntimeError) as e:
                return False, None, f"Path resolution failed: {str(e)}"

            # 4. Check path depth
            if len(real_path.parts) > PathValidator.MAX_DEPTH:
                return False, None, f"Path rejected: too deep (max {PathValidator.MAX_DEPTH} levels)"

            # 5. Check for dangerous patterns
            path_lower = str(real_path).lower()
            for pattern in PathValidator.DANGEROUS_PATTERNS:
                if pattern in path_lower:
                    logger.warning("Path security: Dangerous pattern rejected",
                                 pattern=pattern, path=str(real_path)[:100])
                    return False, None, f"Path rejected: contains restricted pattern '{pattern}'"

            # 6. Check if path is within allowed roots
            is_allowed = any(
                str(real_path).startswith(root)
                for root in PathValidator.ALLOWED_ROOTS
            )

            if not is_allowed:
                # Log attempt to access unauthorized path
                logger.warning("Path security: Unauthorized path rejected",
                             path=str(real_path)[:100],
                             allowed_roots=PathValidator.ALLOWED_ROOTS)
                return False, None, (
                    f"Path rejected: not in allowed directories. "
                    f"Allowed: {', '.join(PathValidator.ALLOWED_ROOTS)}"
                )

            # 7. Check if path exists (optional - depends on use case)
            if not real_path.exists():
                return False, None, f"Path does not exist: {real_path}"

            # 8. Check if it's a directory
            if not real_path.is_dir():
                return False, None, "Path must be a directory"

            # 9. Check read permissions
            if not os.access(real_path, os.R_OK):
                return False, None, "Permission denied: cannot read directory"

            logger.info("Path security: Path validated successfully",
                       path=str(real_path))
            return True, real_path, "Path validated successfully"

        except Exception as e:
            logger.error("Path security: Validation error",
                        input=user_input[:50], error=str(e))
            return False, None, f"Path validation error: {str(e)}"

    @staticmethod
    def create_safe_import_dir(user_id: int, prefix: str = "import") -> Path:
        """
        Create a safe temporary directory for user imports

        Creates directory with:
        - Unique name based on user ID and timestamp
        - Secure permissions (owner only)
        - Located within allowed roots

        Args:
            user_id: Telegram user ID
            prefix: Directory name prefix

        Returns:
            Path to created directory
        """
        timestamp = int(time.time())
        safe_base = Path(PathValidator.ALLOWED_ROOTS[0])

        # Create user-specific directory
        safe_dir = safe_base / f"{prefix}_{user_id}_{timestamp}"
        safe_dir.mkdir(parents=True, exist_ok=True)

        # Set secure permissions (owner read/write/execute only)
        try:
            os.chmod(safe_dir, 0o700)
        except OSError:
            pass  # May fail on some filesystems

        logger.info("Created safe import directory",
                   path=str(safe_dir), user_id=user_id)

        return safe_dir

    @staticmethod
    def is_path_safe(path: str) -> bool:
        """
        Quick check if a path is safe (without full validation)

        Useful for fast filtering before detailed validation
        """
        if not path or not isinstance(path, str):
            return False

        # Quick checks
        if '..' in path:
            return False

        path_lower = path.lower()
        for pattern in PathValidator.DANGEROUS_PATTERNS:
            if pattern in path_lower:
                return False

        return True
