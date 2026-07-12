#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ContextManager hooks: PreCompact custom_instructions override + veto, PostCompact.

The autocompact is monkeypatched so no LLM/threshold math is needed; the test
asserts the hook can override the custom_instructions passed to autocompact, can
veto the whole pass (stop), and that PostCompact fires after a compaction.
"""
from __future__ import annotations

import pytest

from metagpt.common.events import EventBus
from metagpt.common.hook.manager import HookManager
from metagpt.common.hook.subscriber import HookSubscriber
from metagpt.common.schema import AutocompactResult, MicrocompactResult, UserMessage
from metagpt.context.manager import ContextManager


def _fake_micro(messages, config, *, model, compactable):
    return MicrocompactResult(messages=messages, tokens_freed=0)


def _bus_with_hooks(mgr: HookManager) -> EventBus:
    bus = EventBus()
    bus.subscribe(HookSubscriber(mgr))
    return bus


@pytest.mark.asyncio
async def test_precompact_overrides_custom_instructions(monkeypatch):
    captured = {}

    async def fake_auto(messages, llm, config, *, model, tokens_freed, consecutive_failures, custom_instructions):
        captured["ci"] = custom_instructions
        return AutocompactResult(messages=[UserMessage(content="[s]")], compacted=True, summary="sum")

    monkeypatch.setattr("metagpt.context.manager.microcompact", _fake_micro)
    monkeypatch.setattr("metagpt.context.manager.autocompact", fake_auto)

    mgr = HookManager()
    mgr.register("PreCompact", lambda hi: {"additionalContext": "FOCUS ON API"})
    cm = ContextManager(llm=object(), bus=_bus_with_hooks(mgr))
    await cm.add(UserMessage(content="old"))

    await cm.manage_history(custom_instructions="original")
    assert captured["ci"] == "FOCUS ON API"


@pytest.mark.asyncio
async def test_precompact_veto_skips_compaction(monkeypatch):
    called = {"auto": False}

    async def fake_auto(*args, **kwargs):
        called["auto"] = True
        return AutocompactResult(messages=[], compacted=False)

    monkeypatch.setattr("metagpt.context.manager.microcompact", _fake_micro)
    monkeypatch.setattr("metagpt.context.manager.autocompact", fake_auto)

    mgr = HookManager()
    mgr.register("PreCompact", lambda hi: {"continue": False, "stopReason": "not now"})
    cm = ContextManager(llm=object(), bus=_bus_with_hooks(mgr))
    await cm.add(UserMessage(content="old"))

    changed = await cm.manage_history()
    assert changed is False
    assert called["auto"] is False  # vetoed before any pass ran


@pytest.mark.asyncio
async def test_postcompact_fires_after_compaction(monkeypatch):
    fired = []

    async def fake_auto(messages, llm, config, *, model, tokens_freed, consecutive_failures, custom_instructions):
        return AutocompactResult(messages=[UserMessage(content="[s]")], compacted=True, summary="my summary")

    monkeypatch.setattr("metagpt.context.manager.microcompact", _fake_micro)
    monkeypatch.setattr("metagpt.context.manager.autocompact", fake_auto)

    mgr = HookManager()
    mgr.register("PostCompact", lambda hi: fired.append(hi.payload.get("compact_summary")))
    cm = ContextManager(llm=object(), bus=_bus_with_hooks(mgr))
    await cm.add(UserMessage(content="old"))

    await cm.manage_history()
    assert fired == ["my summary"]
