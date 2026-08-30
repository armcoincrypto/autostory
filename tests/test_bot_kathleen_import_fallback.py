"""Wave I: src/bot/bot.py must not hard-crash on missing src.bot.kathleen.

See docs/audits/zollotex-social-agent-phase0-7-20260722T083419Z/KATHLEEN_DECISION.md:
Kathleen has no authoritative source and does not ship. Policy is "no hard
import in entry points" -- already true for web/scheduler/dexpert (guarded
registration), but src/bot/bot.py (the `python main.py bot` entry point)
had a plain top-level import that crashed immediately in this build.
"""
from __future__ import annotations

import importlib


def test_bot_module_imports_successfully() -> None:
    # This is the actual regression: `import src.bot.bot` used to raise
    # ModuleNotFoundError unconditionally before any handler ever ran.
    module = importlib.import_module("src.bot.bot")
    assert hasattr(module, "try_handle_kathleen_message")


def test_try_handle_kathleen_message_available_and_safe() -> None:
    import src.bot.bot as bot_module

    reply = bot_module.try_handle_kathleen_message(12345, "kathleen hello")
    # Whether the real package is present or not, this must never raise and
    # must return either a string reply or None (matches the caller's
    # `if reply is None: return` / `await event.respond(reply)` contract).
    assert reply is None or isinstance(reply, str)
