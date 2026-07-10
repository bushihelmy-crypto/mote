#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ContextManager hooks: PreCompact custom_instructions override + veto, PostCompact.

The reduction now runs through the ``ContextEngine`` (fold → summarize → drop),
so these tests drive a real summarize with a fake LLM and a forced-low threshold
rather than monkeypatching module functions. They assert the PreCompact hook can
override the summarize ``custom_instructions``, can veto the whole pass (stop),
and that PostCompact fires after a compaction with the summary.
"""
from __future__ import annotations

import pytest

import metagpt.context.budget as token_budget
from metagpt.common.events import EventBus
from metagpt.common.hook.manager import HookManager
from metagpt.common.hook.subscriber import HookSubscriber
from metagpt.common.schema import ContextManagerConfig, UserMessage
from metagpt.context.manager import ContextManager


class _FakeLLM:
    """Minimal summarizer: records aask calls and returns a constant summary."""

    def __init__(self, *, summary: str = "sum", model: str = "m"):
        self.model = model
        self._summary = summary
        self.aask_calls: list[dict] = []

    async def aask(self, msg=None, system_msgs=None, stream=True, **kwargs) -> str:
        self.aask_calls.append({"msg": msg, "system_msgs": system_msgs})
        return self._summary


def _summarizing_cfg() -> ContextManagerConfig:
    # No fold, tiny tail -> the summarize reducer carves a head to summarize.
    return ContextManagerConfig(enable_microcompact=False, keep_tail_messages=1, keep_tail_tokens=1)


def _bus_with_hooks(mgr: HookManager) -> EventBus:
    bus = EventBus()
    bus.subscribe(HookSubscriber(mgr))
    return bus


@pytest.fixture(autouse=True)
def _force_threshold(monkeypatch):
    """Force the autocompact threshold low so summarize always fires."""
    monkeypatch.setattr(token_budget, "autocompact_threshold", lambda model: 1)


async def _seed(cm: ContextManager):
    for i in range(6):
        await cm.add(UserMessage(content=f"turn {i} content here"))


@pytest.mark.asyncio
async def test_precompact_overrides_custom_instructions():
    mgr = HookManager()
    mgr.register("PreCompact", lambda hi: {"additionalContext": "FOCUS ON API"})
    cm = ContextManager(llm=_FakeLLM(), config=_summarizing_cfg(), model="m", bus=_bus_with_hooks(mgr))
    await _seed(cm)

    await cm.manage_history(custom_instructions="original")
    # The PreCompact hook's additionalContext reached the summarize reducer,
    # overriding the caller's "original".
    assert cm._summarize.custom_instructions == "FOCUS ON API"


@pytest.mark.asyncio
async def test_precompact_veto_skips_compaction():
    llm = _FakeLLM()
    mgr = HookManager()
    mgr.register("PreCompact", lambda hi: {"continue": False, "stopReason": "not now"})
    cm = ContextManager(llm=llm, config=_summarizing_cfg(), model="m", bus=_bus_with_hooks(mgr))
    await _seed(cm)

    changed = await cm.manage_history()
    assert changed is False
    assert llm.aask_calls == []  # vetoed before the pipeline (and the LLM) ran


@pytest.mark.asyncio
async def test_postcompact_fires_after_compaction():
    fired = []
    mgr = HookManager()
    mgr.register("PostCompact", lambda hi: fired.append(hi.payload.get("compact_summary")))
    cm = ContextManager(llm=_FakeLLM(summary="my summary"), config=_summarizing_cfg(), model="m", bus=_bus_with_hooks(mgr))
    await _seed(cm)

    await cm.manage_history()
    assert fired == ["my summary"]
