#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.context.autocompact`` — LLM summarize + rebuild.

The real autocompact threshold is unreachable with test-sized histories, so the
trigger-dependent tests use the ``force_autocompact_threshold`` fixture (patches
the threshold to 1). Covers: the early-out branches (disabled, circuit breaker,
under threshold, history too short, ``tokens_freed`` enough), the happy path
(``[summary] + tail`` rebuild, prompt selection, failure-count reset, original
list left unmutated) and the two failure paths (summarize raises, empty summary)
that trip the breaker.
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import AutocompactResult, ContextManagerConfig
from metagpt.context.autocompact import autocompact

from .conftest import FakeLLM, text_msg

SHORT_TAIL_CFG = ContextManagerConfig(keep_tail_messages=1, keep_tail_tokens=1, max_consecutive_failures=3)


def _history(n: int) -> list:
    return [text_msg(f"message number {i} with a little content", role="user" if i % 2 == 0 else "assistant") for i in range(n)]


@pytest.mark.asyncio
async def test_disabled_is_noop():
    msgs = _history(6)
    llm = FakeLLM()
    res = await autocompact(msgs, llm, ContextManagerConfig(enable_autocompact=False), model="m")
    assert isinstance(res, AutocompactResult)
    assert not res.compacted
    assert res.messages is msgs
    assert llm.aask_calls == []


@pytest.mark.asyncio
async def test_circuit_breaker_blocks(force_autocompact_threshold):
    msgs = _history(6)
    llm = FakeLLM()
    res = await autocompact(msgs, llm, SHORT_TAIL_CFG, model="m", consecutive_failures=3)
    assert not res.compacted
    assert res.consecutive_failures == 3
    assert llm.aask_calls == []  # breaker tripped before summarizing


@pytest.mark.asyncio
async def test_under_threshold_no_compact():
    # No force fixture → real (huge) threshold; a 4-message history is well under it.
    msgs = _history(4)
    llm = FakeLLM()
    res = await autocompact(msgs, llm, SHORT_TAIL_CFG, model="big-model")
    assert not res.compacted
    assert llm.aask_calls == []
    assert res.pre_compact_tokens >= 0


@pytest.mark.asyncio
async def test_history_too_short(force_autocompact_threshold):
    # Over threshold, but the default keep-tail (5 messages) leaves no head to
    # summarize when only 2 messages exist.
    msgs = _history(2)
    llm = FakeLLM()
    res = await autocompact(msgs, llm, ContextManagerConfig(), model="m")
    assert not res.compacted
    assert llm.aask_calls == []


@pytest.mark.asyncio
async def test_tokens_freed_prevents_trigger(force_autocompact_threshold):
    msgs = _history(6)
    llm = FakeLLM()
    # Freeing a huge amount drops the effective count to 0 (< threshold 1).
    res = await autocompact(msgs, llm, SHORT_TAIL_CFG, model="m", tokens_freed=10_000_000)
    assert not res.compacted
    assert llm.aask_calls == []


@pytest.mark.asyncio
async def test_success_rebuilds_summary_plus_tail(force_autocompact_threshold):
    msgs = _history(6)
    llm = FakeLLM(summary="<summary>all done</summary>")
    res = await autocompact(msgs, llm, SHORT_TAIL_CFG, model="m")
    assert res.compacted
    assert res.summary == "<summary>all done</summary>"
    # rebuilt = [summary message] + preserved tail (1 message, per SHORT_TAIL_CFG)
    assert len(res.messages) == 2
    assert "continued from a previous conversation" in res.messages[0].content
    assert "all done" in res.messages[0].content
    # the preserved tail is the original last message
    assert res.messages[1].content == msgs[-1].content
    # original list untouched (a new list is returned)
    assert res.messages is not msgs
    assert len(msgs) == 6
    # failure counter resets on success
    assert res.consecutive_failures == 0
    assert res.post_compact_tokens > 0


@pytest.mark.asyncio
async def test_success_summarizes_head_with_partial_prompt(force_autocompact_threshold):
    from context import prompt as compact_prompt

    msgs = _history(6)
    llm = FakeLLM()
    await autocompact(msgs, llm, SHORT_TAIL_CFG, model="m")
    assert len(llm.aask_calls) == 1
    call = llm.aask_calls[0]
    # head = everything but the 1-message tail
    assert len(call["msg"]) == 5
    # a tail is preserved → the partial/up_to prompt is used
    assert call["system_msgs"] == [compact_prompt.get_partial_compact_prompt(None)]
    assert call["stream"] is False


@pytest.mark.asyncio
async def test_summarize_failure_trips_breaker(force_autocompact_threshold):
    msgs = _history(6)
    llm = FakeLLM(raise_exc=RuntimeError("boom"))
    res = await autocompact(msgs, llm, SHORT_TAIL_CFG, model="m", consecutive_failures=0)
    assert not res.compacted
    assert res.consecutive_failures == 1
    assert "boom" in res.error
    assert res.messages is msgs  # unchanged


@pytest.mark.asyncio
async def test_empty_summary_trips_breaker(force_autocompact_threshold):
    msgs = _history(6)
    llm = FakeLLM(summary="   ")
    res = await autocompact(msgs, llm, SHORT_TAIL_CFG, model="m", consecutive_failures=1)
    assert not res.compacted
    assert res.consecutive_failures == 2
    assert res.error == "empty summary"


@pytest.mark.asyncio
async def test_model_falls_back_to_llm_model(force_autocompact_threshold):
    msgs = _history(6)
    llm = FakeLLM(model="fake-model")
    # model=None → autocompact uses llm.model; still compacts.
    res = await autocompact(msgs, llm, SHORT_TAIL_CFG, model=None)
    assert res.compacted
