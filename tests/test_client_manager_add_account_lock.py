"""ClientManager.add_account: session lock / orphan wrapper cleanup when connect is cancelled or fails."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def sample_account():
    acc = MagicMock()
    acc.id = 4242
    acc.phone_number = "+10000000042"
    acc.proxy_config = None
    return acc


def _offline_telethon_client_mock():
    """TelegramClient mock: not connected so ``disconnect()`` skips await path."""
    c = MagicMock()
    c.is_connected.return_value = False
    c.disconnect = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_add_account_cancelled_during_connect_releases_wrapper(sample_account):
    from src.clients import manager as mgr

    lock_handle = MagicMock()
    session_obj = MagicMock()

    with patch.object(mgr, "resolve_telethon_session", return_value=(session_obj, "file", None)):
        with patch.object(
            mgr,
            "acquire_session_lock",
            return_value=(True, lock_handle, None),
        ):
            with patch.object(mgr, "TelegramClient", return_value=_offline_telethon_client_mock()):
                cm = mgr.ClientManager()

                async def cancelled(_self):
                    raise asyncio.CancelledError()

                with patch.object(mgr.TelegramClientWrapper, "connect_with_reason", cancelled):
                    with pytest.raises(asyncio.CancelledError):
                        await cm.add_account(sample_account)

                lock_handle.release.assert_called()


@pytest.mark.asyncio
async def test_add_account_exception_during_connect_releases_wrapper(sample_account):
    from src.clients import manager as mgr

    lock_handle = MagicMock()
    session_obj = MagicMock()

    with patch.object(mgr, "resolve_telethon_session", return_value=(session_obj, "file", None)):
        with patch.object(
            mgr,
            "acquire_session_lock",
            return_value=(True, lock_handle, None),
        ):
            with patch.object(mgr, "TelegramClient", return_value=_offline_telethon_client_mock()):
                cm = mgr.ClientManager()

                async def boom(_self):
                    raise RuntimeError("connect boom")

                with patch.object(mgr.TelegramClientWrapper, "connect_with_reason", boom):
                    # Connect exceptions release the orphan wrapper/lock then return
                    # a failure tuple (CancelledError still propagates).
                    wrapper, err = await cm.add_account(sample_account)

                assert wrapper is None
                assert err == "failed_connect"
                lock_handle.release.assert_called()


@pytest.mark.asyncio
async def test_add_account_success_does_not_disconnect_registered_wrapper(sample_account):
    from src.clients import manager as mgr

    lock_handle = MagicMock()
    session_obj = MagicMock()
    disc = AsyncMock()

    with patch.object(mgr, "resolve_telethon_session", return_value=(session_obj, "file", None)):
        with patch.object(
            mgr,
            "acquire_session_lock",
            return_value=(True, lock_handle, None),
        ):
            with patch.object(mgr, "TelegramClient", return_value=_offline_telethon_client_mock()):
                cm = mgr.ClientManager()
                with patch.object(mgr.TelegramClientWrapper, "connect_with_reason", AsyncMock(return_value=(True, None))):
                    with patch.object(mgr.TelegramClientWrapper, "disconnect", disc):
                        w, err = await cm.add_account(sample_account)

                assert err is None
                assert w is not None
                assert cm._clients[sample_account.id] is w
                disc.assert_not_awaited()
                lock_handle.release.assert_not_called()


@pytest.mark.asyncio
async def test_remove_account_unchanged_disconnects_and_drops(sample_account):
    from src.clients import manager as mgr

    lock_handle = MagicMock()
    session_obj = MagicMock()

    with patch.object(mgr, "resolve_telethon_session", return_value=(session_obj, "file", None)):
        with patch.object(
            mgr,
            "acquire_session_lock",
            return_value=(True, lock_handle, None),
        ):
            with patch.object(mgr, "TelegramClient", return_value=_offline_telethon_client_mock()):
                cm = mgr.ClientManager()
                with patch.object(mgr.TelegramClientWrapper, "connect_with_reason", AsyncMock(return_value=(True, None))):
                    w, err = await cm.add_account(sample_account)
                assert err is None

                removed = await cm.remove_account(sample_account.id)
                assert removed is True
                assert sample_account.id not in cm._clients
                lock_handle.release.assert_called()

                removed_again = await cm.remove_account(sample_account.id)
                assert removed_again is False
