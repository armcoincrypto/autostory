"""
Session Manager - Persistent session handling
"""
import os
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
from cryptography.fernet import Fernet
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Account, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)


class SessionManager:
    """
    Manages Telegram session data with encryption support

    Features:
    - Encrypted session storage
    - Session validation
    - Backup and restore
    """

    def __init__(self, encryption_key: Optional[bytes] = None):
        self._session_dir = Path(settings.storage.sessions_dir)
        self._session_dir.mkdir(parents=True, exist_ok=True)

        # Initialize encryption
        if encryption_key:
            self._cipher = Fernet(encryption_key)
        else:
            # Use or generate key
            key_file = self._session_dir / ".key"
            if key_file.exists():
                with open(key_file, "rb") as f:
                    key = f.read()
            else:
                key = Fernet.generate_key()
                with open(key_file, "wb") as f:
                    f.write(key)
                os.chmod(key_file, 0o600)
            self._cipher = Fernet(key)

    def encrypt_session(self, session_string: str) -> str:
        """Encrypt a session string"""
        encrypted = self._cipher.encrypt(session_string.encode())
        return encrypted.decode()

    def decrypt_session(self, encrypted_session: str) -> str:
        """Decrypt a session string"""
        decrypted = self._cipher.decrypt(encrypted_session.encode())
        return decrypted.decode()

    def save_session(
        self,
        account_id: int,
        session_string: str,
        metadata: Optional[Dict] = None
    ) -> bool:
        """Save encrypted session to file"""
        try:
            session_file = self._session_dir / f"session_{account_id}.json"

            data = {
                "account_id": account_id,
                "session": self.encrypt_session(session_string),
                "created_at": datetime.utcnow().isoformat(),
                "metadata": metadata or {}
            }

            with open(session_file, "w") as f:
                json.dump(data, f, indent=2)

            os.chmod(session_file, 0o600)
            logger.info("Session saved", account_id=account_id)
            return True

        except Exception as e:
            logger.error("Failed to save session", account_id=account_id, error=str(e))
            return False

    def load_session(self, account_id: int) -> Optional[str]:
        """Load and decrypt session from file"""
        try:
            session_file = self._session_dir / f"session_{account_id}.json"

            if not session_file.exists():
                return None

            with open(session_file, "r") as f:
                data = json.load(f)

            return self.decrypt_session(data["session"])

        except Exception as e:
            logger.error("Failed to load session", account_id=account_id, error=str(e))
            return None

    def delete_session(self, account_id: int) -> bool:
        """Delete session file"""
        try:
            session_file = self._session_dir / f"session_{account_id}.json"
            if session_file.exists():
                session_file.unlink()
                logger.info("Session deleted", account_id=account_id)
            return True
        except Exception as e:
            logger.error("Failed to delete session", account_id=account_id, error=str(e))
            return False

    def list_sessions(self) -> List[Dict]:
        """List all stored sessions (metadata only)"""
        sessions = []

        for session_file in self._session_dir.glob("session_*.json"):
            try:
                with open(session_file, "r") as f:
                    data = json.load(f)

                sessions.append({
                    "account_id": data["account_id"],
                    "created_at": data["created_at"],
                    "metadata": data.get("metadata", {}),
                    "file": str(session_file)
                })
            except Exception as e:
                logger.warning("Failed to read session file", file=str(session_file), error=str(e))

        return sessions

    def backup_all(self, backup_dir: Path) -> bool:
        """Backup all sessions to a directory"""
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)

            for session_file in self._session_dir.glob("session_*.json"):
                backup_file = backup_dir / session_file.name
                with open(session_file, "r") as src:
                    with open(backup_file, "w") as dst:
                        dst.write(src.read())

            logger.info("Sessions backed up", count=len(list(self._session_dir.glob("session_*.json"))))
            return True

        except Exception as e:
            logger.error("Backup failed", error=str(e))
            return False

    def sync_to_database(self) -> Dict[str, int]:
        """Sync file sessions with database"""
        import os

        if os.environ.get("LEGACY_FERNET_SESSION_MANAGER_ENABLED", "false").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            raise RuntimeError(
                "Legacy Fernet clients.session sync is blocked; use EncryptedSessionText "
                "and migrate_telegram_session_encryption tooling"
            )
        stats = {"created": 0, "updated": 0, "errors": 0}

        with get_db_context() as db:
            for session_info in self.list_sessions():
                try:
                    account = db.query(Account).filter(
                        Account.id == session_info["account_id"]
                    ).first()

                    session_string = self.load_session(session_info["account_id"])

                    if account:
                        if account.session_string != session_string:
                            account.session_string = session_string
                            stats["updated"] += 1
                    else:
                        # Create placeholder account
                        account = Account(
                            id=session_info["account_id"],
                            phone_number=f"unknown_{session_info['account_id']}",
                            session_string=session_string,
                            status=AccountStatus.AUTH_REQUIRED,
                        )
                        db.add(account)
                        stats["created"] += 1

                except Exception as e:
                    logger.error("Sync error", account_id=session_info["account_id"], error=str(e))
                    stats["errors"] += 1

        return stats


# Global session manager instance
session_manager = SessionManager()
