#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ContextManager hooks: PreCompact enrichment and PostCompact observation.

The reduction now runs through the ``ContextEngine`` (fold → summarize → drop),
so these tests drive a real summarize with a fake LLM and a forced-low threshold
rather than monkeypatching module functions. They assert the PreCompact hook can
enrich summarize instructions but cannot veto core compaction, and that
PostCompact fires after a compaction with the summary.
"""
from __future__ import annotations

import pytest

import mote.runtime.context.budget as token_budget
from mote.contracts.schema import ContextManagerConfig, UserMessage
from mote.runtime.context.compaction.policy import build_compaction_policy
from mote.runtime.context.manager import ContextManager
from mote.runtime.hook.manager import HookManager
from mote.runtime.hook.subscriber import HookSubscriber
from mote.ztest.telemetry import InlineTelemetry


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


def _telemetry_with_hooks(mgr: HookManager) -> InlineTelemetry:
    return InlineTelemetry(HookSubscriber(mgr))


@pytest.fixture(autouse=True)
def _force_threshold(monkeypatch):
    """Force the autocompact threshold low so summarize always fires."""
    monkeypatch.setattr(token_budget, "autocompact_threshold", lambda model: 1)


async def _seed(cm: ContextManager):
    for i in range(6):
        await cm.add(UserMessage(content=f"turn {i} content here"))


@pytest.mark.asyncio
async def test_precompact_enriches_custom_instructions():
    mgr = HookManager()
    mgr.register("PreCompact", lambda hi: {"additionalContext": "FOCUS ON API"})
    cm = ContextManager(
        llm=_FakeLLM(),
        config=_summarizing_cfg(),
        model="m",
        telemetry=_telemetry_with_hooks(mgr),
        compaction_policy=build_compaction_policy(hook_manager=mgr),
    )
    await _seed(cm)

    await cm.manage_history(custom_instructions="original")
    assert cm._summarize.custom_instructions == "original\nFOCUS ON API"


@pytest.mark.asyncio
async def test_precompact_veto_is_ignored():
    llm = _FakeLLM()
    mgr = HookManager()
    mgr.register("PreCompact", lambda hi: {"continue": False, "stopReason": "not now"})
    cm = ContextManager(
        llm=llm,
        config=_summarizing_cfg(),
        model="m",
        telemetry=_telemetry_with_hooks(mgr),
        compaction_policy=build_compaction_policy(hook_manager=mgr),
    )
    await _seed(cm)

    changed = await cm.manage_history()
    assert changed is True
    assert len(llm.aask_calls) == 1


@pytest.mark.asyncio
async def test_postcompact_fires_after_compaction():
    fired = []
    mgr = HookManager()
    mgr.register("PostCompact", lambda hi: fired.append(hi.payload.get("compact_summary")))
    cm = ContextManager(
        llm=_FakeLLM(summary="my summary"),
        config=_summarizing_cfg(),
        model="m",
        telemetry=_telemetry_with_hooks(mgr),
    )
    await _seed(cm)

    await cm.manage_history()
    assert fired == ["my summary"]
