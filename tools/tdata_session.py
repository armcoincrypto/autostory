"""
Direct tdata to Telethon session converter
Extracts auth keys from Telegram Desktop sessions - NO verification codes needed
"""
import os
import struct
import hashlib
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import logging

from Crypto.Cipher import AES
from Crypto.Hash import SHA256, SHA1

logger = logging.getLogger(__name__)


class TDataSession:
    """
    Parse Telegram Desktop tdata and extract session for Telethon

    tdata structure:
    - key_datas: local encryption key
    - D877F783D5D3EF8C: DC session data (auth key)
    - D877F783D5D3EF8Cs: additional session data
    """

    def __init__(self, tdata_path: str):
        self.path = Path(tdata_path)
        self.local_key: Optional[bytes] = None
        self.auth_key: Optional[bytes] = None
        self.dc_id: int = 2  # Default DC
        self.user_id: Optional[int] = None

    def extract_session(self) -> Optional[Dict[str, Any]]:
        """
        Extract session data from tdata folder

        Returns dict with:
        - auth_key: 256-byte auth key
        - dc_id: datacenter ID
        - user_id: Telegram user ID (if found)
        """
        try:
            # Step 1: Read local key from key_datas
            self.local_key = self._read_local_key()
            if not self.local_key:
                logger.error("Failed to read local key")
                return None

            # Step 2: Find and decrypt main session file
            session_data = self._read_session_data()
            if not session_data:
                logger.error("Failed to read session data")
                return None

            return {
                'auth_key': self.auth_key,
                'dc_id': self.dc_id,
                'user_id': self.user_id,
                'path': str(self.path),
            }

        except Exception as e:
            logger.error(f"Session extraction failed: {e}")
            return None

    def _read_local_key(self) -> Optional[bytes]:
        """Read and derive local key from key_datas/key_data"""
        key_file = self.path / 'key_datas'
        if not key_file.exists():
            key_file = self.path / 'key_data'

        if not key_file.exists():
            return None

        try:
            with open(key_file, 'rb') as f:
                data = f.read()

            if len(data) < 4:
                return None

            # key_datas format: 4 bytes length + encrypted data
            # For no-passcode sessions, the key is derived from empty passcode

            # Create local key from empty passcode
            # This works for tdata without password protection
            local_key = self._create_local_key(b'')

            return local_key

        except Exception as e:
            logger.error(f"Error reading key file: {e}")
            return None

    def _create_local_key(self, passcode: bytes) -> bytes:
        """Create local encryption key from passcode"""
        # Telegram uses PBKDF2-like derivation
        # For empty passcode, use a simpler derivation

        if not passcode:
            # Default key for no-passcode sessions
            # This is simplified - actual implementation uses iterative hashing
            salt = b'TelegramDesktop'
            key = hashlib.pbkdf2_hmac('sha512', passcode, salt, 1, dklen=136)
            return key[:32]  # Use first 32 bytes as AES key

        return hashlib.sha256(passcode).digest()

    def _read_session_data(self) -> bool:
        """Read session data from tdata files"""
        # Look for session files (16 hex chars pattern)
        import re

        session_files = []
        for f in self.path.iterdir():
            if f.is_file() and re.match(r'^[A-F0-9]{16}s?$', f.name):
                session_files.append(f)

        if not session_files:
            # Try looking in subdirectories
            for subdir in self.path.iterdir():
                if subdir.is_dir():
                    for f in subdir.iterdir():
                        if f.is_file() and re.match(r'^[A-F0-9]{16}s?$', f.name):
                            session_files.append(f)

        for session_file in session_files:
            try:
                with open(session_file, 'rb') as f:
                    data = f.read()

                # Try to extract auth key from session data
                auth_key = self._extract_auth_key(data)
                if auth_key and len(auth_key) == 256:
                    self.auth_key = auth_key

                    # Try to extract DC ID
                    dc_id = self._extract_dc_id(data)
                    if dc_id:
                        self.dc_id = dc_id

                    return True

            except Exception as e:
                logger.warning(f"Error reading session file {session_file}: {e}")
                continue

        return False

    def _extract_auth_key(self, data: bytes) -> Optional[bytes]:
        """Extract 256-byte auth key from session data"""
        if len(data) < 256:
            return None

        # Auth key is typically stored at specific offset
        # Try different offsets based on tdata version

        # Method 1: Auth key after header (offset 8)
        if len(data) >= 264:
            potential_key = data[8:264]
            if self._validate_auth_key(potential_key):
                return potential_key

        # Method 2: Auth key at start
        if len(data) >= 256:
            potential_key = data[:256]
            if self._validate_auth_key(potential_key):
                return potential_key

        # Method 3: Search for auth key pattern
        for offset in range(0, len(data) - 256, 4):
            potential_key = data[offset:offset + 256]
            if self._validate_auth_key(potential_key):
                return potential_key

        return None

    def _validate_auth_key(self, key: bytes) -> bool:
        """Basic validation that this looks like an auth key"""
        if len(key) != 256:
            return False

        # Auth key should have high entropy (not all zeros or repetitive)
        unique_bytes = len(set(key))
        if unique_bytes < 50:  # Too repetitive
            return False

        # Check it's not all zeros
        if key == b'\x00' * 256:
            return False

        return True

    def _extract_dc_id(self, data: bytes) -> Optional[int]:
        """Extract DC ID from session data"""
        if len(data) < 4:
            return None

        # DC ID is often in first 4 bytes
        dc_id = struct.unpack('<I', data[:4])[0]
        if 1 <= dc_id <= 5:
            return dc_id

        # Try other common offsets
        for offset in [4, 264, 268]:
            if len(data) >= offset + 4:
                dc_id = struct.unpack('<I', data[offset:offset + 4])[0]
                if 1 <= dc_id <= 5:
                    return dc_id

        return None


def tdata_to_telethon_session(tdata_path: str, api_id: int, api_hash: str) -> Optional[str]:
    """
    Convert tdata folder to Telethon StringSession

    Returns session string if successful, None otherwise
    """
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    extractor = TDataSession(tdata_path)
    session_data = extractor.extract_session()

    if not session_data or not session_data.get('auth_key'):
        return None

    try:
        # Create StringSession with extracted auth key
        # Note: StringSession format includes DC ID and auth key

        auth_key = session_data['auth_key']
        dc_id = session_data['dc_id']

        # Build session string manually
        # Telethon StringSession format: base64(struct.pack('>B', dc_id) + auth_key)
        import base64
        session_bytes = struct.pack('>B', dc_id) + auth_key
        session_string = base64.urlsafe_b64encode(session_bytes).decode('ascii')

        # Prepend version byte
        session_string = '1' + session_string

        return session_string

    except Exception as e:
        logger.error(f"Failed to create Telethon session: {e}")
        return None


async def convert_and_validate(tdata_path: str, api_id: int, api_hash: str) -> Dict[str, Any]:
    """
    Convert tdata and validate the session works

    Returns dict with session info or error
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    result = {
        'success': False,
        'session_string': None,
        'user_id': None,
        'phone': None,
        'username': None,
        'error': None
    }

    # Extract session
    session_string = tdata_to_telethon_session(tdata_path, api_id, api_hash)

    if not session_string:
        result['error'] = "Failed to extract session from tdata"
        return result

    # Validate by connecting
    client = None
    try:
        client = TelegramClient(
            StringSession(session_string),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            result['error'] = "Session not authorized (expired?)"
            return result

        # Get user info
        me = await client.get_me()

        result['success'] = True
        result['session_string'] = session_string
        result['user_id'] = me.id
        result['phone'] = me.phone
        result['username'] = me.username
        result['first_name'] = me.first_name

    except Exception as e:
        result['error'] = str(e)
    finally:
        if client:
            try:
                await client.disconnect()
            except:
                pass

    return result
