"""
SECURITY HOTFIX: Cryptographically secure random generation
Fixes HIGH vulnerability: Insecure Hash Algorithm (MD5)
"""
import hashlib
import secrets
import time
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)


class SecureCrypto:
    """
    Secure cryptographic utilities

    Replaces insecure MD5 usage with:
    - secrets module for random generation (cryptographically secure)
    - SHA-256 for hashing when needed
    """

    @staticmethod
    def generate_session_id(length: int = 32) -> str:
        """
        Generate cryptographically secure session ID

        Uses Python's secrets module which is designed for
        generating cryptographically strong random numbers.

        Args:
            length: Number of bytes (output will be 2x in hex)

        Returns:
            Hex string of random bytes (64 chars for 32 bytes)
        """
        return secrets.token_hex(length)

    @staticmethod
    def generate_session_name(phone: str, length: int = 20) -> str:
        """
        Generate secure session name for Telegram sessions

        Security improvements over MD5:
        - Uses SHA-256 (not broken)
        - Includes cryptographic random salt
        - Includes timestamp for uniqueness

        Args:
            phone: Phone number (used as input)
            length: Output length (truncated from hash)

        Returns:
            Secure session name string
        """
        # Generate cryptographic random salt
        salt = secrets.token_hex(16)

        # Add timestamp for additional uniqueness
        timestamp = str(int(time.time() * 1000000))

        # Combine inputs
        input_str = f"{phone}_{salt}_{timestamp}"

        # Use SHA-256 (secure, unlike MD5)
        hash_result = hashlib.sha256(input_str.encode()).hexdigest()

        # Return truncated result
        return hash_result[:length]

    @staticmethod
    def generate_token(length: int = 32) -> str:
        """
        Generate URL-safe random token

        Suitable for:
        - API tokens
        - Password reset links
        - Verification codes

        Args:
            length: Number of bytes

        Returns:
            URL-safe base64 encoded token
        """
        return secrets.token_urlsafe(length)

    @staticmethod
    def generate_secret_key(length: int = 32) -> str:
        """
        Generate secret key for Flask/cryptographic use

        Args:
            length: Number of bytes (output is 2x in hex)

        Returns:
            Hex string suitable for secret key
        """
        key = secrets.token_hex(length)
        logger.info("Generated new secret key", length=len(key))
        return key

    @staticmethod
    def hash_file(file_path: str, algorithm: str = 'sha256') -> Optional[str]:
        """
        Generate secure hash of a file

        Args:
            file_path: Path to file
            algorithm: Hash algorithm (sha256, sha512)

        Returns:
            Hex digest or None if file not readable
        """
        if algorithm not in ('sha256', 'sha512'):
            algorithm = 'sha256'

        try:
            hasher = hashlib.new(algorithm)

            with open(file_path, 'rb') as f:
                # Read in chunks to handle large files
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)

            return hasher.hexdigest()

        except (IOError, OSError) as e:
            logger.error("File hash failed", path=file_path, error=str(e))
            return None

    @staticmethod
    def secure_compare(a: str, b: str) -> bool:
        """
        Constant-time string comparison

        Prevents timing attacks when comparing secrets.

        Args:
            a: First string
            b: Second string

        Returns:
            True if strings are equal
        """
        return secrets.compare_digest(a, b)

    @staticmethod
    def generate_numeric_code(length: int = 6) -> str:
        """
        Generate random numeric code (for verification)

        Args:
            length: Number of digits

        Returns:
            Numeric string of specified length
        """
        # Generate random number in range
        max_val = 10 ** length
        code = secrets.randbelow(max_val)

        # Pad with zeros if needed
        return str(code).zfill(length)
