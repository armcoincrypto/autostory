"""
User Discovery - Scan public chats and discover users
"""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Set

from telethon import functions
from telethon.tl.types import (
    Channel,
    Chat,
    User,
    ChannelParticipantsSearch,
    ChannelParticipantsRecent,
)
from telethon.errors import ChatAdminRequiredError, ChannelPrivateError
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import DiscoveredUser
from src.core.database import get_db_context
from src.clients.manager import TelegramClientWrapper, client_manager
from src.clients.rate_limiter import AntiDetection

logger = structlog.get_logger(__name__)


class ChatScanner:
    """
    Scans public chats/channels to discover users

    Features:
    - Scan channel members
    - Scan message senders
    - Filter users by criteria
    - Avoid duplicates
    """

    def __init__(self):
        self._seen_users: Set[int] = set()
        self._load_existing_users()

    def _load_existing_users(self) -> None:
        """Load existing user IDs from database"""
        with get_db_context() as db:
            users = db.query(DiscoveredUser.user_id).all()
            self._seen_users = {u[0] for u in users}
        logger.info("Loaded existing users", count=len(self._seen_users))

    async def scan_channel_members(
        self,
        client_wrapper: TelegramClientWrapper,
        channel_username: str,
        limit: int = 1000,
        filter_bots: bool = True,
    ) -> Dict[str, Any]:
        """
        Scan channel members

        Args:
            client_wrapper: Telegram client
            channel_username: Channel @username or ID
            limit: Maximum members to fetch
            filter_bots: Exclude bot accounts

        Returns:
            Scan results with discovered users
        """
        client = client_wrapper.client
        results = {
            "success": False,
            "channel": channel_username,
            "total_scanned": 0,
            "new_users": 0,
            "errors": []
        }

        try:
            # Get channel entity
            channel = await client.get_entity(channel_username)

            if not isinstance(channel, (Channel, Chat)):
                results["errors"].append("Not a channel or chat")
                return results

            channel_id = channel.id
            channel_title = getattr(channel, 'title', channel_username)

            logger.info(
                "Scanning channel members",
                channel=channel_title,
                limit=limit
            )

            # Fetch participants
            new_users = []
            offset = 0
            batch_size = 100

            while offset < limit:
                await AntiDetection.random_pause(1, 3)

                try:
                    participants = await client(functions.channels.GetParticipantsRequest(
                        channel=channel,
                        filter=ChannelParticipantsRecent(),
                        offset=offset,
                        limit=min(batch_size, limit - offset),
                        hash=0
                    ))

                    if not participants.users:
                        break

                    for user in participants.users:
                        if self._should_include_user(user, filter_bots):
                            if user.id not in self._seen_users:
                                discovered = self._create_discovered_user(
                                    user, channel_id, channel_title
                                )
                                new_users.append(discovered)
                                self._seen_users.add(user.id)

                        results["total_scanned"] += 1

                    offset += len(participants.users)

                    if len(participants.users) < batch_size:
                        break

                except ChatAdminRequiredError:
                    results["errors"].append("Admin access required")
                    break
                except Exception as e:
                    results["errors"].append(str(e))
                    break

            # Save to database
            if new_users:
                with get_db_context() as db:
                    db.add_all(new_users)
                    db.commit()

            results["success"] = True
            results["new_users"] = len(new_users)

            logger.info(
                "Channel scan complete",
                channel=channel_title,
                scanned=results["total_scanned"],
                new=results["new_users"]
            )

        except ChannelPrivateError:
            results["errors"].append("Channel is private")
        except Exception as e:
            results["errors"].append(str(e))
            logger.error("Channel scan failed", error=str(e))

        return results

    async def scan_chat_messages(
        self,
        client_wrapper: TelegramClientWrapper,
        chat_username: str,
        days_back: int = 7,
        limit: int = 500,
        filter_bots: bool = True,
    ) -> Dict[str, Any]:
        """
        Scan users from chat messages

        Args:
            client_wrapper: Telegram client
            chat_username: Chat @username or ID
            days_back: Look back this many days
            limit: Maximum messages to scan
            filter_bots: Exclude bot accounts

        Returns:
            Scan results
        """
        client = client_wrapper.client
        results = {
            "success": False,
            "chat": chat_username,
            "messages_scanned": 0,
            "new_users": 0,
            "errors": []
        }

        try:
            # Get chat entity
            chat = await client.get_entity(chat_username)
            chat_id = chat.id
            chat_title = getattr(chat, 'title', chat_username)

            logger.info(
                "Scanning chat messages",
                chat=chat_title,
                days_back=days_back
            )

            # Calculate date range
            min_date = datetime.utcnow() - timedelta(days=days_back)

            # Iterate through messages
            new_users = []
            async for message in client.iter_messages(
                chat,
                limit=limit,
                offset_date=datetime.utcnow()
            ):
                if message.date.replace(tzinfo=None) < min_date:
                    break

                results["messages_scanned"] += 1

                # Get sender
                if message.sender and isinstance(message.sender, User):
                    user = message.sender
                    if self._should_include_user(user, filter_bots):
                        if user.id not in self._seen_users:
                            discovered = self._create_discovered_user(
                                user, chat_id, chat_title
                            )
                            new_users.append(discovered)
                            self._seen_users.add(user.id)

                # Rate limit
                if results["messages_scanned"] % 100 == 0:
                    await AntiDetection.random_pause(0.5, 1.5)

            # Save to database
            if new_users:
                with get_db_context() as db:
                    db.add_all(new_users)
                    db.commit()

            results["success"] = True
            results["new_users"] = len(new_users)

            logger.info(
                "Chat message scan complete",
                chat=chat_title,
                messages=results["messages_scanned"],
                new_users=results["new_users"]
            )

        except Exception as e:
            results["errors"].append(str(e))
            logger.error("Chat message scan failed", error=str(e))

        return results

    def _should_include_user(self, user: User, filter_bots: bool) -> bool:
        """Check if user should be included"""
        if user.deleted:
            return False
        if filter_bots and user.bot:
            return False
        if not user.id:
            return False
        return True

    def _create_discovered_user(
        self,
        user: User,
        source_chat_id: int,
        source_chat_title: str
    ) -> DiscoveredUser:
        """Create DiscoveredUser model from Telegram user"""
        return DiscoveredUser(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            source_chat_id=source_chat_id,
            source_chat_title=source_chat_title,
            discovered_at=datetime.utcnow(),
        )


class UserDiscovery:
    """
    High-level user discovery orchestration

    Features:
    - Multi-channel scanning
    - Scheduled discovery
    - Statistics and reporting
    """

    def __init__(self):
        self._scanner = ChatScanner()

    async def discover_from_channels(
        self,
        channel_usernames: List[str],
        limit_per_channel: int = 500,
    ) -> Dict[str, Any]:
        """
        Discover users from multiple channels

        Args:
            channel_usernames: List of channel usernames
            limit_per_channel: Max users per channel

        Returns:
            Aggregated results
        """
        results = {
            "success": False,
            "channels_processed": 0,
            "total_scanned": 0,
            "total_new_users": 0,
            "channel_results": [],
            "errors": []
        }

        # Get available client
        clients = await client_manager.get_available_clients()
        if not clients:
            results["errors"].append("No available clients")
            return results

        client_wrapper = clients[0]

        for channel in channel_usernames:
            try:
                scan_result = await self._scanner.scan_channel_members(
                    client_wrapper,
                    channel,
                    limit=limit_per_channel
                )

                results["channel_results"].append(scan_result)
                results["channels_processed"] += 1
                results["total_scanned"] += scan_result.get("total_scanned", 0)
                results["total_new_users"] += scan_result.get("new_users", 0)

                if scan_result.get("errors"):
                    results["errors"].extend(scan_result["errors"])

                # Wait between channels
                await asyncio.sleep(5)

            except Exception as e:
                results["errors"].append(f"{channel}: {str(e)}")

        results["success"] = results["channels_processed"] > 0
        return results

    async def get_discovery_stats(self) -> Dict[str, Any]:
        """Get discovery statistics"""
        with get_db_context() as db:
            total = db.query(DiscoveredUser).count()
            mentioned = db.query(DiscoveredUser).filter(
                DiscoveredUser.times_mentioned > 0
            ).count()
            unmentioned = total - mentioned

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
            "mentioned": mentioned,
            "unmentioned": unmentioned,
            "discovered_this_week": recent,
            "top_sources": [{"chat": s[0], "count": s[1]} for s in sources]
        }

    async def get_users_for_mention(
        self,
        count: int = 5,
        exclude_mentioned: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get users suitable for mentioning"""
        with get_db_context() as db:
            query = db.query(DiscoveredUser).filter(
                DiscoveredUser.is_blocked == False
            )

            if exclude_mentioned:
                query = query.filter(DiscoveredUser.times_mentioned == 0)

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
chat_scanner = ChatScanner()
user_discovery = UserDiscovery()
