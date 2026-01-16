"""
Session Manager - Persistent session storage with Redis
Ensures sessions survive restarts
"""
import asyncio
import json
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from cryptography.fernet import Fernet
import os

from telethon import TelegramClient
from telethon.sessions import StringSession
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Account, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)

# Try to import Redis, but work without it
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not available, using database-only session storage")


class SessionManager:
    """
    Manages Telegram session persistence

    Features:
    - Store sessions in Redis for fast access
    - Encrypt session data
    - Auto-restore sessions on startup
    - Health checks for logged-in accounts
    """

    def __init__(self):
        self._clients: Dict[int, TelegramClient] = {}
        self._redis: Optional[Any] = None
        self._encryption_key: Optional[bytes] = None

        # Initialize encryption
        self._init_encryption()

        # Initialize Redis if available
        if REDIS_AVAILABLE:
            self._init_redis()

    def _init_encryption(self):
        """Initialize encryption key"""
        key_file = "data/session.key"
        os.makedirs("data", exist_ok=True)

        if os.path.exists(key_file):
            with open(key_file, "rb") as f:
                self._encryption_key = f.read()
        else:
            self._encryption_key = Fernet.generate_key()
            with open(key_file, "wb") as f:
                f.write(self._encryption_key)

        self._fernet = Fernet(self._encryption_key)

    def _init_redis(self):
        """Initialize Redis connection"""
        try:
            self._redis = redis.Redis(
                host=settings.redis.host,
                port=settings.redis.port,
                db=0,
                decode_responses=False,
            )
            self._redis.ping()
            logger.info("Redis connected successfully")
        except Exception as e:
            logger.warning("Redis connection failed, using database only", error=str(e))
            self._redis = None

    def _encrypt(self, data: str) -> bytes:
        """Encrypt session data"""
        return self._fernet.encrypt(data.encode())

    def _decrypt(self, data: bytes) -> str:
        """Decrypt session data"""
        return self._fernet.decrypt(data).decode()

    async def save_session(self, account_id: int, session_string: str):
        """Save session to Redis and database with encryption fallback"""
        # Try to encrypt
        try:
            encrypted = self._encrypt(session_string)
        except Exception as e:
            logger.warning("Encryption failed, saving plain", error=str(e))
            encrypted = session_string.encode()

        # Save to Redis with 7-day TTL
        if self._redis:
            try:
                self._redis.setex(
                    f"session:{account_id}",
                    timedelta(days=7),
                    encrypted
                )
                logger.debug("Session saved to Redis", account_id=account_id)
            except Exception as e:
                logger.warning("Failed to save to Redis", error=str(e))

        # Also save to database (backup)
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    account.session_string = session_string
                    account.last_active = datetime.utcnow()
                    db.commit()
        except Exception as e:
            logger.error("Failed to save session to database", error=str(e))

    async def get_session(self, account_id: int) -> Optional[str]:
        """Get session from Redis or database"""
        # Try Redis first
        if self._redis:
            try:
                encrypted = self._redis.get(f"session:{account_id}")
                if encrypted:
                    return self._decrypt(encrypted)
            except Exception as e:
                logger.warning("Failed to get from Redis", error=str(e))

        # Fallback to database
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account and account.session_string:
                    # Re-cache to Redis
                    await self.save_session(account_id, account.session_string)
                    return account.session_string
        except Exception as e:
            logger.error("Failed to get session from database", error=str(e))

        return None

    async def get_client(self, account_id: int) -> Optional[TelegramClient]:
        """Get or create a connected client for an account"""
        # Return existing client if connected
        if account_id in self._clients:
            client = self._clients[account_id]
            if client.is_connected():
                try:
                    # Verify still authorized
                    if await client.is_user_authorized():
                        return client
                except:
                    pass
            # Remove dead client
            del self._clients[account_id]

        # Get session
        session_string = await self.get_session(account_id)
        if not session_string:
            return None

        # Create new client
        try:
            client = TelegramClient(
                StringSession(session_string),
                settings.telegram.api_id,
                settings.telegram.api_hash
            )

            await client.connect()

            if await client.is_user_authorized():
                self._clients[account_id] = client
                return client
            else:
                logger.warning("Session no longer authorized", account_id=account_id)
                await self._mark_account_needs_reauth(account_id)
                return None

        except Exception as e:
            logger.error("Failed to create client", account_id=account_id, error=str(e))
            return None

    async def restore_all_sessions(self) -> Dict[str, Any]:
        """Restore all sessions on startup"""
        results = {
            "total": 0,
            "restored": 0,
            "failed": 0,
            "accounts": [],
        }

        with get_db_context() as db:
            accounts = db.query(Account).filter(
                Account.session_string.isnot(None)
            ).all()
            results["total"] = len(accounts)

            for account in accounts:
                try:
                    client = await self.get_client(account.id)
                    if client:
                        results["restored"] += 1
                        results["accounts"].append({
                            "id": account.id,
                            "phone": account.phone_number,
                            "status": "ok"
                        })
                    else:
                        results["failed"] += 1
                        results["accounts"].append({
                            "id": account.id,
                            "phone": account.phone_number,
                            "status": "failed"
                        })
                except Exception as e:
                    results["failed"] += 1
                    logger.error("Failed to restore session", account_id=account.id, error=str(e))

        logger.info(
            "Session restoration complete",
            total=results["total"],
            restored=results["restored"],
            failed=results["failed"]
        )

        return results

    async def _mark_account_needs_reauth(self, account_id: int):
        """Mark account as needing re-authentication"""
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    account.status = AccountStatus.AUTH_REQUIRED
                    db.commit()
        except Exception as e:
            logger.error("Failed to mark account", error=str(e))

    async def close_all(self):
        """Close all client connections"""
        for account_id, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except:
                pass
        self._clients.clear()
        logger.info("All sessions closed")


# Global instance
session_manager = SessionManager()
