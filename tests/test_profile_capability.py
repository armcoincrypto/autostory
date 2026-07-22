"""Profile mutation capability is persisted separately from health/story when Telegram blocks UploadProfilePhoto."""
import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_profile_photo_frozen_method_sets_restricted():
    from src.clients import manager as mgr

    class Boom(Exception):
        pass

    boom = Boom("You tried to use a method that is not available for frozen accounts")

    class FakeClient:
        async def upload_file(self, _path):
            return b"up"

        async def __call__(self, _req):
            raise boom

    fake_wrapper = MagicMock()
    fake_wrapper.is_connected = True
    fake_wrapper.client = FakeClient()

    mdb = MagicMock()
    acc = MagicMock()
    mdb.query.return_value.filter.return_value.first.return_value = acc

    @contextmanager
    def mock_db():
        yield mdb

    async def run():
        cm = mgr.ClientManager()
        with patch.object(cm, "get_client", new_callable=AsyncMock, return_value=fake_wrapper):
            with patch.object(mgr, "get_db_context", mock_db):
                return await cm.set_account_profile_photo(7, "/tmp/x.jpg")

    r = asyncio.run(run())
    assert r.get("success") is False
    assert r.get("profile_capability_status") == "restricted"
    assert "frozen" in (r.get("profile_capability_reason") or "").lower()
    assert acc.profile_capability_status == "restricted"
    mdb.commit.assert_called()


def test_telegram_profile_mutation_restricted_detector():
    from src.clients import manager as mgr

    assert mgr._telegram_profile_mutation_restricted(
        "You tried to use a method that is not available for frozen accounts"
    )
    assert not mgr._telegram_profile_mutation_restricted("random failure")
