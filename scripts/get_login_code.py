#!/usr/bin/env python3
"""
Get Telegram login code from an account session (for logging in on phone without SMS).

Use when: You have an account in the database (e.g. from tdata import) and want to
log it into your phone, but can't receive SMS. Choose "Send code via Telegram"
on the phone - the code will arrive at this session.

Usage:
  python -m scripts.get_login_code ACCOUNT_ID
  python -m scripts.get_login_code 1

Steps:
  1. Run this script with your account ID
  2. On your phone: Telegram → Add account → enter phone number
  3. When asked, choose "Send code via Telegram" (not SMS)
  4. The script will show the code when it arrives (wait up to 2 minutes)
  5. Enter the code on your phone to complete login
"""
import asyncio
import os
import re
import sys
import time
from pathlib import Path

# Project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from config.settings import settings
from src.core.database import get_db_context
from src.core.models import Account


# Telegram sends login codes from user ID 777000 (Telegram service)
TELEGRAM_USER_ID = 777000
CODE_PATTERN = re.compile(r"\b(\d{5})\b")  # 5-digit code


async def wait_for_login_code(account_id: int, timeout_sec: int = 120) -> str | None:
    """
    Connect to account, poll for login code from Telegram, return it when found.
    """
    api_id = getattr(settings.telegram, "api_id", None) or int(os.environ.get("TELEGRAM_API_ID", 0) or 0)
    api_hash = getattr(settings.telegram, "api_hash", None) or os.environ.get("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        raise ValueError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH required. "
            "Run: ./sync_db_from_server.sh root@207.180.212.142 --env"
        )

    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise SystemExit(f"Account {account_id} not found")
        if not account.session_string:
            raise SystemExit(f"Account {account_id} has no session (tdata/import required)")
        session_str = account.session_string
        phone = account.phone_number or f"#{account_id}"

    client = TelegramClient(
        StringSession(session_str),
        api_id,
        api_hash,
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Account session is not authorized. Re-import or add the account.")

    print(f"Account: {phone}")
    print("On your phone: Add account → enter number → choose 'Send code via Telegram'")
    print(f"Waiting up to {timeout_sec}s for the code...\n")

    # Login codes arrive from Telegram (777000) or in Saved Messages
    peers_to_check = []
    seen_peers = set()
    for entity in [TELEGRAM_USER_ID, "Telegram", "me"]:
        try:
            p = await client.get_input_entity(entity)
            pid = getattr(p, 'user_id', None) or getattr(p, 'channel_id', 0) or str(p)
            if pid not in seen_peers:
                seen_peers.add(pid)
                peers_to_check.append(p)
        except Exception:
            continue
    if not peers_to_check:
        peers_to_check = [await client.get_input_entity(TELEGRAM_USER_ID)]

    result: list[str] = []
    seen_ids: set[tuple[int | str, int]] = set()
    deadline = time.monotonic() + timeout_sec

    @client.on(events.NewMessage(from_users=[TELEGRAM_USER_ID]))
    async def on_new_message(event):
        if event.text:
            m = CODE_PATTERN.search(event.text)
            if m:
                result.append(m.group(1))

    client.add_event_handler(on_new_message)

    try:
        while time.monotonic() < deadline:
            if result:
                await client.disconnect()
                return result[0]
            try:
                for peer in peers_to_check:
                    messages = await client.get_messages(peer, limit=15)
                    for msg in messages:
                        pid = getattr(peer, 'user_id', None) or getattr(peer, 'channel_id', 0) or 0
                        key = (pid, msg.id)
                        if key in seen_ids or not msg.text:
                            continue
                        seen_ids.add(key)
                        match = CODE_PATTERN.search(msg.text)
                        if match:
                            await client.disconnect()
                            return match.group(1)
            except Exception as e:
                print(f"Poll error: {e}")
            await asyncio.sleep(2)
    finally:
        client.remove_event_handler(on_new_message)

    await client.disconnect()
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.get_login_code ACCOUNT_ID")
        print("  Example: python -m scripts.get_login_code 1")
        sys.exit(1)
    try:
        account_id = int(sys.argv[1])
    except ValueError:
        print("ACCOUNT_ID must be a number")
        sys.exit(1)
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 120

    try:
        code = asyncio.run(wait_for_login_code(account_id, timeout))
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    if code:
        print(f"\n--- Login code: {code} ---\n")
        print("Enter this code on your phone to complete login.")
    else:
        print("\nNo code received in time. Try again: request the code on your phone, then rerun this script.")


if __name__ == "__main__":
    main()
