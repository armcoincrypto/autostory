"""
User Discovery - Scan public chats and discover users from messages
Enhanced version with 1-year scanning and user validation
"""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Set

from telethon import functions, TelegramClient
from telethon.tl.types import (
    Channel,
    Chat,
    User,
    ChannelParticipantsSearch,
    ChannelParticipantsRecent,
    UserStatusEmpty,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
)
from telethon.errors import (
    ChatAdminRequiredError,
    ChannelPrivateError,
    FloodWaitError,
    UserNotParticipantError,
)
import structlog

import os
import sys
_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_here, '..', '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from config.settings import settings
from src.core.models import DiscoveredUser, Account, AccountStatus
from src.core.database import get_db_context
from src.clients.rate_limiter import AntiDetection

logger = structlog.get_logger(__name__)


class GroupMessageScanner:
    """
    Advanced scanner that collects users from group message history

    Features:
    - Scan 1 year of message history
    - Deduplicate users
    - Validate deleted/deactivated users
    - Progress tracking
    - Rate limiting to avoid bans
    """

    def __init__(self):
        self._seen_users: Set[int] = set()
        self._load_existing_users()

    def _load_existing_users(self) -> None:
        """Load existing user IDs from database to avoid duplicates"""
        try:
            with get_db_context() as db:
                users = db.query(DiscoveredUser.user_id).all()
                self._seen_users = {u[0] for u in users}
            logger.info("Loaded existing users", count=len(self._seen_users))
        except Exception as e:
            logger.error("Failed to load existing users", error=str(e))
            self._seen_users = set()

    async def get_active_client(self, entity_hint=None) -> Optional[TelegramClient]:
        """
        Get an active Telegram client from database, trying accounts until one works.
        If entity_hint is provided (username or ID), verify the account can resolve it.
        """
        from telethon.sessions import StringSession, SQLiteSession

        with get_db_context() as db:
            accounts = db.query(Account).filter(
                Account.status == AccountStatus.ACTIVE,
                Account.session_string.isnot(None),
            ).all()
            session_pairs = [(a.id, a.session_string) for a in accounts]

        for account_id, session_string in session_pairs:
            try:
                # session_string stores a file path (e.g. /opt/.../account_13.session)
                # SQLiteSession expects path without the .session extension
                if session_string.startswith('/') or session_string.endswith('.session'):
                    sess_path = session_string.removesuffix('.session')
                    session = SQLiteSession(sess_path)
                else:
                    session = StringSession(session_string)

                client = TelegramClient(
                    session,
                    settings.telegram.api_id,
                    settings.telegram.api_hash,
                )
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    continue

                if entity_hint is not None:
                    try:
                        await client.get_entity(entity_hint)
                    except Exception:
                        await client.disconnect()
                        continue  # this account can't see the entity, try next

                return client
            except Exception as e:
                logger.warning("Skipping account with unusable session",
                               account_id=account_id, error=str(e))
                continue

        return None

    async def scan_group_messages(
        self,
        group_username: str,
        days_back: int = 365,
        limit: int = 500,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """
        Scan group messages for the last N days and collect users

        Args:
            group_username: Group @username or invite link
            days_back: Days to look back (default 365 = 1 year)
            progress_callback: Async function to report progress

        Returns:
            Scan results with statistics
        """
        results = {
            "success": False,
            "group": group_username,
            "group_title": "",
            "messages_scanned": 0,
            "unique_users_found": 0,
            "new_users_saved": 0,
            "deleted_users_skipped": 0,
            "bots_skipped": 0,
            "duplicates_skipped": 0,
            "errors": [],
            "duration_seconds": 0,
        }

        start_time = datetime.utcnow()

        # Get client — pass group_username so we skip accounts that can't see this entity
        client = await self.get_active_client(entity_hint=group_username)
        if not client:
            results["errors"].append("No active Telegram account with access to this group.")
            return results

        try:
            # Get group entity (already verified in get_active_client)
            try:
                group = await client.get_entity(group_username)
            except Exception as e:
                results["errors"].append(f"Cannot access group: {str(e)}")
                return results

            group_id = group.id
            group_title = getattr(group, 'title', group_username)
            results["group_title"] = group_title

            logger.info(
                "Starting group scan",
                group=group_title,
                days_back=days_back
            )

            if progress_callback:
                await progress_callback(f"🔍 Scanning **{group_title}**...\nThis may take a while for 1 year of messages.")

            # Calculate date range
            min_date = datetime.utcnow() - timedelta(days=days_back)

            # Track users found in this scan
            users_in_scan: Dict[int, User] = {}
            message_count = 0
            last_progress = 0

            # Iterate through messages (capped by limit to avoid Gunicorn timeout)
            async for message in client.iter_messages(
                group,
                limit=limit,
                offset_date=datetime.utcnow(),
                reverse=False,  # Newest first
            ):
                # Check if we've gone past our date range
                if message.date.replace(tzinfo=None) < min_date:
                    break

                message_count += 1
                results["messages_scanned"] = message_count

                # Extract sender
                if message.sender_id and message.sender:
                    sender = message.sender
                    if isinstance(sender, User):
                        if sender.id not in users_in_scan:
                            users_in_scan[sender.id] = sender

                # Also check for forwards
                if message.forward and message.forward.sender_id:
                    try:
                        fwd_sender = await message.forward.get_sender()
                        if isinstance(fwd_sender, User):
                            if fwd_sender.id not in users_in_scan:
                                users_in_scan[fwd_sender.id] = fwd_sender
                    except:
                        pass

                # Progress update every 1000 messages
                if progress_callback and message_count - last_progress >= 1000:
                    last_progress = message_count
                    await progress_callback(
                        f"📊 Progress: {message_count:,} messages scanned\n"
                        f"👥 Unique users found: {len(users_in_scan):,}"
                    )

                # Rate limiting - pause every 500 messages
                if message_count % 500 == 0:
                    await asyncio.sleep(1)

                # Handle flood wait

            results["unique_users_found"] = len(users_in_scan)

            if progress_callback:
                await progress_callback(
                    f"✅ Scan complete!\n"
                    f"📊 {message_count:,} messages scanned\n"
                    f"👥 {len(users_in_scan):,} unique users found\n\n"
                    f"🔄 Processing and validating users..."
                )

            # Process and validate users
            new_users = []
            for user_id, user in users_in_scan.items():
                # Check if deleted
                if user.deleted:
                    results["deleted_users_skipped"] += 1
                    continue

                # Check if bot
                if user.bot:
                    results["bots_skipped"] += 1
                    continue

                # Check if already in database
                if user_id in self._seen_users:
                    results["duplicates_skipped"] += 1
                    continue

                # Validate user is real (has some activity indicator)
                if not self._is_valid_user(user):
                    results["deleted_users_skipped"] += 1
                    continue

                # Create discovered user record
                discovered = DiscoveredUser(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    source_chat_id=group_id,
                    source_chat_title=group_title,
                    discovered_at=datetime.utcnow(),
                )
                new_users.append(discovered)
                self._seen_users.add(user.id)

            # Save to database in batches
            if new_users:
                batch_size = 100
                for i in range(0, len(new_users), batch_size):
                    batch = new_users[i:i + batch_size]
                    try:
                        with get_db_context() as db:
                            db.add_all(batch)
                            db.commit()
                        results["new_users_saved"] += len(batch)
                    except Exception as e:
                        logger.error("Failed to save batch", error=str(e))
                        results["errors"].append(f"Save error: {str(e)}")

            results["success"] = True
            results["duration_seconds"] = (datetime.utcnow() - start_time).total_seconds()

            logger.info(
                "Group scan complete",
                group=group_title,
                messages=results["messages_scanned"],
                new_users=results["new_users_saved"]
            )

        except FloodWaitError as e:
            results["errors"].append(f"Rate limited. Wait {e.seconds} seconds.")
            logger.warning("Flood wait", seconds=e.seconds)
        except ChannelPrivateError:
            results["errors"].append("Group is private. Join first.")
        except Exception as e:
            results["errors"].append(str(e))
            logger.error("Scan failed", error=str(e))
        finally:
            await client.disconnect()

        return results

    def _is_valid_user(self, user: User) -> bool:
        """
        Validate that a user is real and active

        Checks:
        - Not deleted
        - Not a bot
        - Has some identifier (username or name)
        - Has been seen recently (if status available)
        """
        if user.deleted:
            return False

        if user.bot:
            return False

        # Must have at least username or first_name
        if not user.username and not user.first_name:
            return False

        # Check status if available (indicates real account)
        if user.status:
            # These statuses indicate active/real users
            valid_statuses = (
                UserStatusOnline,
                UserStatusOffline,
                UserStatusRecently,
                UserStatusLastWeek,
                UserStatusLastMonth,
            )
            if not isinstance(user.status, valid_statuses):
                # UserStatusEmpty often means deleted/deactivated
                if isinstance(user.status, UserStatusEmpty):
                    return False

        return True

    async def scan_multiple_groups(
        self,
        group_usernames: List[str],
        days_back: int = 365,
        limit_per_channel: int = 500,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """Scan multiple groups sequentially"""
        total_results = {
            "success": False,
            "channels_processed": 0,
            "groups_scanned": 0,
            "total_messages": 0,
            "total_new_users": 0,
            "group_results": [],
            "errors": [],
        }

        for i, group in enumerate(group_usernames):
            if progress_callback:
                await progress_callback(
                    f"📂 Scanning group {i+1}/{len(group_usernames)}: {group}"
                )

            result = await self.scan_group_messages(
                group,
                days_back=days_back,
                limit=limit_per_channel,
                progress_callback=progress_callback,
            )

            total_results["group_results"].append(result)
            total_results["groups_scanned"] += 1
            total_results["channels_processed"] += 1
            total_results["total_messages"] += result.get("messages_scanned", 0)
            total_results["total_new_users"] += result.get("new_users_saved", 0)

            if result.get("errors"):
                total_results["errors"].extend(result["errors"])

            # Wait between groups to avoid rate limits
            if i < len(group_usernames) - 1:
                await asyncio.sleep(10)

        total_results["success"] = total_results["groups_scanned"] > 0
        return total_results


class UserDiscovery:
    """
    High-level user discovery orchestration
    """

    def __init__(self):
        self._scanner = GroupMessageScanner()

    async def discover_from_group(
        self,
        group_username: str,
        days_back: int = 365,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """Discover users from a single group"""
        return await self._scanner.scan_group_messages(
            group_username,
            days_back=days_back,
            progress_callback=progress_callback,
        )

    async def discover_from_groups(
        self,
        group_usernames: List[str],
        days_back: int = 365,
        limit_per_channel: int = 500,
        progress_callback=None,
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Discover users from multiple groups"""
        from src.core.execution_guard import (
            ACTION_DISCOVERY_SCAN,
            guard_blocked_discovery,
            require_execution_allowed,
        )

        blocked = require_execution_allowed(ACTION_DISCOVERY_SCAN, dry_run=dry_run)
        if blocked is not None:
            logger.warning("discovery_scan_blocked_execution_guard", reason=blocked.reason_code)
            return guard_blocked_discovery(blocked)

        return await self._scanner.scan_multiple_groups(
            group_usernames,
            days_back=days_back,
            limit_per_channel=limit_per_channel,
            progress_callback=progress_callback,
        )

    async def discover_from_channels(
        self,
        channel_usernames: List[str],
        limit_per_channel: int = 500,
        days_back: int = 365,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """Alias for discover_from_groups — supports channel and group usernames."""
        return await self.discover_from_groups(
            group_usernames=channel_usernames,
            days_back=days_back,
            limit_per_channel=limit_per_channel,
            progress_callback=progress_callback,
        )

    async def join_channel(self, channel: str, *, dry_run: bool = False) -> Dict[str, Any]:
        """
        Join a Telegram channel/group using the first available active account.
        This is needed so that account sessions cache the access hash for
        private/ID-based channel resolution (required for scanning).
        """
        from src.core.execution_guard import (
            ACTION_DISCOVERY_JOIN,
            guard_blocked_discovery,
            require_execution_allowed,
        )

        blocked = require_execution_allowed(ACTION_DISCOVERY_JOIN, dry_run=dry_run)
        if blocked is not None:
            logger.warning("discovery_join_blocked_execution_guard", reason=blocked.reason_code)
            return guard_blocked_discovery(blocked)

        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest

        client = await self._scanner.get_active_client()
        if not client:
            return {'success': False, 'error': 'No active Telegram account available'}

        try:
            # Handle invite links like t.me/joinchat/... or t.me/+...
            if 'joinchat/' in channel or channel.startswith('https://t.me/+'):
                hash_part = channel.split('/')[-1].lstrip('+')
                await client(ImportChatInviteRequest(hash_part))
                return {'success': True, 'title': channel}

            entity = await client.get_entity(channel)
            await client(JoinChannelRequest(entity))
            return {'success': True, 'title': getattr(entity, 'title', channel)}
        except Exception as e:
            return {'success': False, 'error': str(e)}
        finally:
            await client.disconnect()

    async def get_discovery_stats(self) -> Dict[str, Any]:
        """Get discovery statistics"""
        with get_db_context() as db:
            total = db.query(DiscoveredUser).count()
            mentioned = db.query(DiscoveredUser).filter(
                DiscoveredUser.times_mentioned > 0
            ).count()
            unmentioned = total - mentioned

            with_username = db.query(DiscoveredUser).filter(
                DiscoveredUser.username.isnot(None)
            ).count()

            # Recent discoveries
            week_ago = datetime.utcnow() - timedelta(days=7)
            recent = db.query(DiscoveredUser).filter(
                DiscoveredUser.discovered_at >= week_ago
            ).count()

            # Top sources
            from sqlalchemy import func
            sources = db.query(
                DiscoveredUser.source_chat_title,
                func.count(DiscoveredUser.id)
            ).group_by(DiscoveredUser.source_chat_title).order_by(
                func.count(DiscoveredUser.id).desc()
            ).limit(10).all()

        return {
            "total_discovered": total,
            "with_username": with_username,
            "mentioned": mentioned,
            "unmentioned": unmentioned,
            "discovered_this_week": recent,
            "top_sources": [{"chat": s[0], "count": s[1]} for s in sources]
        }

    async def get_users_for_mention(
        self,
        count: int = 5,
        require_username: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get users suitable for mentioning in stories"""
        with get_db_context() as db:
            query = db.query(DiscoveredUser).filter(
                DiscoveredUser.is_blocked == False,
                DiscoveredUser.times_mentioned == 0,
            )

            if require_username:
                query = query.filter(DiscoveredUser.username.isnot(None))

            users = query.order_by(
                DiscoveredUser.discovered_at.desc()
            ).limit(count).all()

            return [
                {
                    "user_id": u.user_id,
                    "username": u.username,
                    "first_name": u.first_name,
                    "source": u.source_chat_title,
                }
                for u in users
            ]


# Global instances
group_scanner = GroupMessageScanner()
user_discovery = UserDiscovery()
