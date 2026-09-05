"""Wave 7A — Telethon runtime loop affinity + dialogs→history stability."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.clients import telethon_runtime as tr

ROOT = Path(__file__).resolve().parents[1]


class LoopBoundFakeClient:
    """Models Telethon's loop affinity: ops fail if called on a different loop."""

    def __init__(self) -> None:
        self.bound_loop = None
        self.connect_count = 0
        self.op_count = 0
        self.entities = {}

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        if self.bound_loop is None:
            self.bound_loop = loop
        elif self.bound_loop is not loop:
            raise RuntimeError(
                "The asyncio event loop must not change after connection "
                "(see the FAQ for details)"
            )
        self.connect_count += 1

    async def get_dialogs(self, limit: int = 10):
        await self.connect()
        self.op_count += 1
        # populate entity cache as Telethon would after dialogs
        self.entities[111] = SimpleNamespace(id=111, title="Peer")
        return [SimpleNamespace(entity=self.entities[111])]

    async def get_entity(self, peer):
        await self.connect()
        self.op_count += 1
        key = int(peer) if str(peer).lstrip("-").isdigit() else peer
        if key not in self.entities and int(key) not in self.entities:
            raise ValueError(f"Could not find the input entity for {peer}")
        return self.entities.get(key) or self.entities.get(int(key))

    async def iter_messages(self, entity, limit: int = 20):
        await self.connect()
        self.op_count += 1

        async def _gen():
            yield SimpleNamespace(
                id=1, message="hello", date=None, out=False
            )

        async for m in _gen():
            yield m


@pytest.fixture
def isolated_runtime(monkeypatch):
    """Fresh telethon_runtime for each test (no cross-test loop reuse)."""
    tr.shutdown(timeout=2.0)
    # reset module globals explicitly
    with tr._lock:
        tr._loop = None
        tr._thread = None
        tr._started = False
        tr._stopping = False
    yield
    tr.shutdown(timeout=2.0)
    with tr._lock:
        tr._loop = None
        tr._thread = None
        tr._started = False
        tr._stopping = False


def test_legacy_new_event_loop_per_request_fails_on_cached_client():
    """Pre-fix regression model: dialogs on loop A, history on loop B → boom."""
    client = LoopBoundFakeClient()

    async def dialogs():
        await client.get_dialogs()
        return True

    async def history():
        await client.get_entity(111)
        return True

    loop_a = asyncio.new_event_loop()
    try:
        assert loop_a.run_until_complete(dialogs()) is True
    finally:
        loop_a.close()

    loop_b = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="event loop must not change"):
            loop_b.run_until_complete(history())
    finally:
        loop_b.close()


def test_persistent_runtime_dialogs_then_history(isolated_runtime):
    client = LoopBoundFakeClient()

    async def dialogs():
        await client.get_dialogs()
        return "dialogs-ok"

    async def history():
        ent = await client.get_entity(111)
        assert ent.id == 111
        return "history-ok"

    assert tr.run(dialogs()) == "dialogs-ok"
    assert tr.run(history()) == "history-ok"
    # Same loop for both
    assert client.connect_count >= 2
    assert client.bound_loop is not None
    # Repeated sequence
    assert tr.run(dialogs()) == "dialogs-ok"
    assert tr.run(history()) == "history-ok"


def test_persistent_runtime_repeated_and_multi_account(isolated_runtime):
    clients = {1: LoopBoundFakeClient(), 2: LoopBoundFakeClient()}

    async def work(aid: int, kind: str):
        c = clients[aid]
        if kind == "dialogs":
            await c.get_dialogs()
        else:
            await c.get_dialogs()
            await c.get_entity(111)
        return f"{aid}-{kind}"

    for _ in range(3):
        assert tr.run(work(1, "dialogs")).startswith("1-")
        assert tr.run(work(1, "history")).startswith("1-")
        assert tr.run(work(2, "dialogs")).startswith("2-")
        assert tr.run(work(2, "history")).startswith("2-")

    assert clients[1].bound_loop is clients[2].bound_loop


def test_concurrent_reads_same_runtime(isolated_runtime):
    client = LoopBoundFakeClient()
    errors: list[BaseException] = []

    async def op(n: int):
        await client.get_dialogs()
        await client.get_entity(111)
        return n

    def call(n: int):
        try:
            return tr.run(op(n))
        except BaseException as e:
            errors.append(e)
            raise

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(call, i) for i in range(8)]
        results = [f.result(timeout=10) for f in futs]
    assert sorted(results) == list(range(8))
    assert errors == []


def test_routes_run_async_uses_telethon_runtime(isolated_runtime, monkeypatch):
    from src.dashboard import routes as routes_mod

    seen = {}

    async def sample():
        seen["loop"] = asyncio.get_running_loop()
        return 42

    assert routes_mod.run_async(sample()) == 42
    assert tr.is_running()
    assert seen["loop"] is tr._loop


def test_messages_run_async_uses_same_runtime(isolated_runtime):
    from src.dashboard import messages_routes as mr

    loops = []

    async def sample():
        loops.append(asyncio.get_running_loop())
        return "ok"

    assert mr._run_async(sample()) == "ok"
    assert mr._run_async(sample()) == "ok"
    assert loops[0] is loops[1]
    assert loops[0] is tr._loop


def test_resolve_peer_warms_dialogs_on_miss():
    from src.messaging import transport as transport_mod

    class FakeClient:
        def __init__(self):
            self.warmed = False
            self.entities = {}

        async def get_entity(self, peer):
            key = int(peer)
            if key not in self.entities:
                raise ValueError("Could not find the input entity")
            return self.entities[key]

    client = FakeClient()
    calls = {"dialogs": 0}

    async def fake_get_dialogs(account_id, limit=200, **kwargs):
        calls["dialogs"] += 1
        client.entities[1980718196] = SimpleNamespace(id=1980718196)
        return []

    async def _run():
        # patch client_manager.get_dialogs used by warm helper
        import src.messaging.transport as t

        original = t.client_manager.get_dialogs
        t.client_manager.get_dialogs = fake_get_dialogs  # type: ignore
        try:
            ent = await t._resolve_peer_with_dialog_warm(106, client, "1980718196")
            assert ent.id == 1980718196
            assert calls["dialogs"] == 1
        finally:
            t.client_manager.get_dialogs = original

    asyncio.run(_run())


def test_shutdown_stops_runtime(isolated_runtime):
    async def ping():
        return "pong"

    assert tr.run(ping()) == "pong"
    tr.shutdown(timeout=5.0)
    assert tr.is_running() is False


def test_no_access_hash_in_dialogs_or_history_api_source():
    hist = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    dlg = (ROOT / "src/clients/manager.py").read_text(encoding="utf-8")
    # response construction must not include access_hash
    start = dlg.index("async def get_dialogs")
    chunk = dlg[start : start + 5000]
    assert "access_hash" not in chunk[chunk.index("for d in dialogs") :]
    assert "access_hash" not in hist
    assert "telethon_run" in hist or "telethon_runtime" in hist


def test_owner_history_error_copy_normalized():
    src = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert "Unable to load conversation history." in src
    assert "HISTORY_UNAVAILABLE" in src
