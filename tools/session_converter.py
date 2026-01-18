"""
Convert Telethon .session files to StringSession
Much simpler than tdata - these are already Telethon format!
"""
import os
import sqlite3
import struct
import base64
from pathlib import Path
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


def session_file_to_string(session_path: str) -> Optional[str]:
    """
    Convert a Telethon .session SQLite file to StringSession format

    .session files are SQLite databases with the session data
    """
    try:
        conn = sqlite3.connect(session_path)
        cursor = conn.cursor()

        # Get session data from the sessions table
        cursor.execute("SELECT dc_id, server_address, port, auth_key FROM sessions")
        row = cursor.fetchone()

        if not row:
            conn.close()
            return None

        dc_id, server_address, port, auth_key = row

        conn.close()

        if not auth_key or len(auth_key) != 256:
            logger.warning(f"Invalid auth_key length: {len(auth_key) if auth_key else 0}")
            return None

        # Build StringSession format
        # Format: version (1 byte) + dc_id (1 byte) + server_address + port (2 bytes) + auth_key (256 bytes)
        # StringSession v1 format

        # Encode server address
        ip_bytes = bytes([int(x) for x in server_address.split('.')])

        # Pack the data
        # Format: dc_id (1 byte) + ip (4 bytes) + port (2 bytes) + auth_key (256 bytes)
        data = struct.pack('>B', dc_id)  # 1 byte dc_id
        data += ip_bytes  # 4 bytes IP
        data += struct.pack('>H', port)  # 2 bytes port
        data += auth_key  # 256 bytes auth key

        # Base64 encode
        session_string = '1' + base64.urlsafe_b64encode(data).decode('ascii')

        return session_string

    except sqlite3.Error as e:
        logger.error(f"SQLite error reading {session_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error converting session {session_path}: {e}")
        return None


def get_session_info_from_json(json_path: str) -> Dict[str, Any]:
    """Extract session info from companion .json file"""
    import json
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
            return {
                'phone': data.get('phone') or data.get('phone_number'),
                'api_id': data.get('app_id') or data.get('api_id'),
                'api_hash': data.get('app_hash') or data.get('api_hash'),
                'first_name': data.get('first_name'),
                'last_name': data.get('last_name'),
            }
    except:
        return {}


def discover_session_files(root_path: str) -> List[Dict[str, Any]]:
    """
    Find all .session files in a directory

    Returns list of dicts with:
    - session_path: path to .session file
    - phone: phone number (from filename or .json)
    - json_path: path to companion .json if exists
    - api_id: API ID from .json (if available)
    - api_hash: API hash from .json (if available)
    """
    root = Path(root_path)
    sessions = []

    # Find all .session files
    for session_file in root.rglob('*.session'):
        phone = session_file.stem  # Filename without extension
        api_id = None
        api_hash = None
        first_name = None

        # Look for companion .json file
        json_path = session_file.with_suffix('.json')
        if json_path.exists():
            json_info = get_session_info_from_json(str(json_path))
            if json_info.get('phone'):
                phone = json_info['phone']
            api_id = json_info.get('api_id')
            api_hash = json_info.get('api_hash')
            first_name = json_info.get('first_name')

        sessions.append({
            'session_path': str(session_file),
            'phone': phone,
            'json_path': str(json_path) if json_path.exists() else None,
            'size': session_file.stat().st_size,
            'api_id': api_id,
            'api_hash': api_hash,
            'first_name': first_name,
        })

    return sessions


async def convert_and_validate_session(
    session_path: str,
    api_id: int,
    api_hash: str,
    phone_hint: Optional[str] = None,
    json_path: Optional[str] = None,
    use_original_api: bool = True
) -> Dict[str, Any]:
    """
    Convert .session file and validate it works

    If use_original_api=True and json_path has API credentials,
    uses those instead (sessions are bound to their original API ID)

    Returns dict with session info
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    result = {
        'success': False,
        'session_string': None,
        'user_id': None,
        'phone': phone_hint,
        'username': None,
        'first_name': None,
        'error': None,
        'api_id_used': api_id,
    }

    # Try to get original API credentials from JSON
    actual_api_id = api_id
    actual_api_hash = api_hash

    if use_original_api and json_path:
        json_info = get_session_info_from_json(json_path)
        if json_info.get('api_id') and json_info.get('api_hash'):
            actual_api_id = json_info['api_id']
            actual_api_hash = json_info['api_hash']
            result['api_id_used'] = actual_api_id
            logger.info(f"Using original API ID {actual_api_id} from JSON")

    # Convert to string session
    session_string = session_file_to_string(session_path)

    if not session_string:
        result['error'] = "Failed to extract session from file"
        return result

    # Validate by connecting
    client = None
    try:
        client = TelegramClient(
            StringSession(session_string),
            actual_api_id,
            actual_api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            result['error'] = "Session not authorized"
            return result

        # Get user info
        me = await client.get_me()

        result['success'] = True
        result['session_string'] = session_string
        result['user_id'] = me.id
        result['phone'] = me.phone or phone_hint
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
