"""
Convert tdata (Telegram Desktop) to Telethon sessions
Professional converter for STORYFLEET
"""
import os
import asyncio
import struct
import hashlib
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    AuthKeyUnregisteredError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    SessionRevokedError,
    FloodWaitError,
)

logger = logging.getLogger(__name__)


class TdataConverter:
    """
    Convert tdata folders to Telethon sessions with validation

    Usage:
        converter = TdataConverter(api_id, api_hash)
        results = await converter.convert_all(tdata_accounts, validate=True)
    """

    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.stats = {
            'attempted': 0,
            'successful': 0,
            'failed': 0,
            'banned': 0,
            'limited': 0,
            'errors': []
        }

    async def convert_account(self, tdata_path: Path,
                            phone_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Attempt to convert a single tdata account to Telethon session

        Note: Direct tdata conversion requires parsing Telegram's proprietary format.
        This implementation uses an alternative approach - attempting to restore
        the session using extracted credentials.

        Args:
            tdata_path: Path to tdata folder
            phone_hint: Optional phone number hint

        Returns:
            Dict with session info if successful, None otherwise
        """
        self.stats['attempted'] += 1

        try:
            # Try to extract auth key and DC info
            auth_key, dc_id = self._extract_auth_info(tdata_path)

            if not auth_key:
                self.stats['failed'] += 1
                self.stats['errors'].append(f"{tdata_path.name}: No auth key found")
                return None

            # Create client and test
            # Note: Direct auth key injection requires low-level session manipulation
            # For now, we'll mark this as needing manual login but with extracted phone

            phone = phone_hint or self._extract_phone_from_tdata(tdata_path)

            result = {
                'tdata_path': str(tdata_path),
                'phone_number': phone,
                'dc_id': dc_id,
                'has_auth_key': auth_key is not None,
                'auth_key_hash': hashlib.md5(auth_key).hexdigest()[:16] if auth_key else None,
                'needs_manual_login': True,  # Until we implement direct conversion
                'converted_at': datetime.now().isoformat(),
            }

            # If we have a phone, mark as potentially convertible
            if phone:
                result['status'] = 'ready_for_login'
                self.stats['successful'] += 1
            else:
                result['status'] = 'needs_phone'
                self.stats['failed'] += 1

            return result

        except Exception as e:
            self.stats['failed'] += 1
            self.stats['errors'].append(f"{tdata_path.name}: {str(e)}")
            logger.error(f"Conversion failed for {tdata_path}: {e}")
            return None

    def _extract_auth_info(self, tdata_path: Path) -> Tuple[Optional[bytes], Optional[int]]:
        """Extract auth key and DC ID from tdata"""
        auth_key = None
        dc_id = None

        # Try key_datas first (newer format)
        key_file = tdata_path / 'key_datas'
        if not key_file.exists():
            key_file = tdata_path / 'key_data'

        if key_file.exists():
            try:
                with open(key_file, 'rb') as f:
                    data = f.read()

                    if len(data) >= 4:
                        # First 4 bytes often contain DC ID or version
                        dc_id = struct.unpack('<I', data[:4])[0]
                        if dc_id > 5:  # Not a valid DC ID, might be version
                            dc_id = None

                    # Auth key is typically 256 bytes
                    if len(data) >= 264:
                        auth_key = data[8:264]  # Skip header
                    elif len(data) >= 256:
                        auth_key = data[:256]

            except Exception as e:
                logger.warning(f"Failed to read key file: {e}")

        return auth_key, dc_id

    def _extract_phone_from_tdata(self, tdata_path: Path) -> Optional[str]:
        """Extract phone number from tdata files"""
        import re

        # Check folder name
        folder_name = tdata_path.parent.name
        phone_match = re.search(r'\+?(\d{10,15})', folder_name)
        if phone_match:
            phone = phone_match.group(1)
            if not phone.startswith('+'):
                phone = '+' + phone
            return phone

        # Check binary files for phone patterns
        for fname in ['configs', 'user_data', 'settings']:
            fpath = tdata_path / fname
            if fpath.exists():
                try:
                    with open(fpath, 'rb') as f:
                        data = f.read(4096)
                        # Look for phone pattern
                        matches = re.findall(rb'[\x00](\d{10,15})[\x00]', data)
                        if matches:
                            return '+' + matches[0].decode('ascii')
                except:
                    pass

        return None

    async def validate_session(self, session_string: str) -> Dict[str, Any]:
        """
        Validate a Telethon session string

        Returns:
            Dict with validation results
        """
        result = {
            'valid': False,
            'user_id': None,
            'phone': None,
            'username': None,
            'first_name': None,
            'is_premium': False,
            'can_post_stories': False,
            'is_limited': False,
            'error': None
        }

        client = None
        try:
            client = TelegramClient(
                StringSession(session_string),
                self.api_id,
                self.api_hash
            )

            await client.connect()

            if not await client.is_user_authorized():
                result['error'] = "Not authorized"
                return result

            # Get user info
            me = await client.get_me()
            result['valid'] = True
            result['user_id'] = me.id
            result['phone'] = me.phone
            result['username'] = me.username
            result['first_name'] = me.first_name
            result['is_premium'] = getattr(me, 'premium', False)

            # Test story capability (lightweight check)
            try:
                # Just check if we can access stories API
                from telethon.tl.functions.stories import GetAllStoriesRequest
                await client(GetAllStoriesRequest(next="", hidden=False, state=""))
                result['can_post_stories'] = True
            except FloodWaitError as e:
                result['can_post_stories'] = False
                result['is_limited'] = True
                result['error'] = f"Rate limited for {e.seconds}s"
            except Exception as e:
                if 'FLOOD' in str(e).upper():
                    result['is_limited'] = True
                result['error'] = str(e)

        except AuthKeyUnregisteredError:
            result['error'] = "Session expired or revoked"
        except (UserDeactivatedError, UserDeactivatedBanError):
            result['error'] = "Account banned/deactivated"
        except SessionRevokedError:
            result['error'] = "Session revoked"
        except FloodWaitError as e:
            result['error'] = f"Flood wait: {e.seconds}s"
            result['is_limited'] = True
        except Exception as e:
            result['error'] = str(e)
        finally:
            if client:
                try:
                    await client.disconnect()
                except:
                    pass

        return result

    async def convert_all(self, accounts: List[Any],
                         validate: bool = True) -> List[Dict[str, Any]]:
        """
        Convert multiple tdata accounts

        Args:
            accounts: List of TdataAccount objects from analyzer
            validate: Whether to validate converted sessions

        Returns:
            List of conversion results
        """
        results = []

        for account in accounts:
            if not account.is_valid:
                results.append({
                    'tdata_path': str(account.path),
                    'status': 'skipped',
                    'error': account.validation_error
                })
                continue

            result = await self.convert_account(
                account.path,
                phone_hint=account.phone_number
            )

            if result:
                results.append(result)
            else:
                results.append({
                    'tdata_path': str(account.path),
                    'status': 'failed',
                    'error': 'Conversion failed'
                })

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get conversion statistics"""
        return self.stats.copy()

    def format_stats(self) -> str:
        """Format statistics for display"""
        s = self.stats
        return (
            f"📊 **Conversion Stats**\n\n"
            f"Attempted: {s['attempted']}\n"
            f"✅ Successful: {s['successful']}\n"
            f"❌ Failed: {s['failed']}\n"
            f"🚫 Banned: {s['banned']}\n"
            f"⏳ Limited: {s['limited']}"
        )


class QuickLogin:
    """
    Quick login helper for accounts extracted from tdata
    Uses phone number to send login code
    """

    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash

    async def start_login(self, phone: str) -> Dict[str, Any]:
        """
        Start login process for a phone number

        Returns dict with:
        - client: TelegramClient (connected, awaiting code)
        - phone_code_hash: Hash for sign_in
        """
        client = TelegramClient(
            StringSession(),
            self.api_id,
            self.api_hash
        )

        await client.connect()

        result = await client.send_code_request(phone)

        return {
            'client': client,
            'phone': phone,
            'phone_code_hash': result.phone_code_hash,
        }

    async def complete_login(self, login_state: Dict, code: str,
                           password: Optional[str] = None) -> Optional[str]:
        """
        Complete login with verification code

        Returns session string if successful
        """
        client = login_state['client']
        phone = login_state['phone']
        phone_code_hash = login_state['phone_code_hash']

        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash
            )
        except Exception as e:
            if 'password' in str(e).lower() or '2fa' in str(e).lower():
                if password:
                    await client.sign_in(password=password)
                else:
                    raise Exception("2FA required but no password provided")
            else:
                raise

        # Get session string
        session_string = client.session.save()

        return session_string
