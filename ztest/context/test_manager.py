#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.context.manager.ContextManager`` — the facade.

Two responsibilities:

1. Message store (the slice of the old ``Memory`` the loop depends on):
   ``get`` / ``add`` / ``add_batch`` / ``delete`` / ``count`` / ``clear`` /
   ``messages`` — all backed by the injected ``LLMCallContext`` so the history is
   checkpointed.
2. History orchestration: ``manage_history`` runs microcompact then autocompact,
   ``token_state`` reports the budget, and ``prepare_request`` assembles the
   per-call request (managed history + the user prompt, without storing it).

Compaction-triggering tests reuse ``force_autocompact_threshold`` and small
configs so the real gates fire on tiny inputs.
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import ContextManagerConfig, LLMCallContext, UserMessage
from context import ContextManager

from .conftest import FakeLLM, make_pairs, text_msg

MICRO_CFG = ContextManagerConfig(
    microcompact_trigger_threshold=2,
    microcompact_keep_recent=1,
    enable_autocompact=False,
)


# ---------------------------------------------------------------------------
# Message-store API
# ---------------------------------------------------------------------------


def test_add_and_count_and_get_all():
    cm = ContextManager(model="gpt-4")
    cm.add(text_msg("a"))
    cm.add(text_msg("b"))
    assert cm.count() == 2
    assert [m.content for m in cm.get()] == ["a", "b"]


def test_add_skips_none():
    cm = ContextManager(model="gpt-4")
    cm.add(None)
    assert cm.count() == 0


def test_add_batch_skips_falsy():
    cm = ContextManager(model="gpt-4")
    cm.add_batch([text_msg("a"), None, text_msg("b")])
    assert cm.count() == 2


def test_get_k_returns_tail():
    cm = ContextManager(model="gpt-4")
    cm.add_batch([text_msg(str(i)) for i in range(5)])
    assert [m.content for m in cm.get(2)] == ["3", "4"]
    assert [m.content for m in cm.get(0)] == ["0", "1", "2", "3", "4"]


def test_delete_present_and_absent():
    cm = ContextManager(model="gpt-4")
    m = text_msg("a")
    cm.add(m)
    cm.delete(m)
    assert cm.count() == 0
    # deleting again (absent) is a safe no-op
    cm.delete(m)
    assert cm.count() == 0


def test_clear():
    cm = ContextManager(model="gpt-4")
    cm.add_batch([text_msg("a"), text_msg("b")])
    cm.clear()
    assert cm.count() == 0


def test_messages_backs_injected_context():
    ctx = LLMCallContext()
    cm = ContextManager(ctx, model="gpt-4")
    cm.add(text_msg("a"))
    # the store mutates the shared context (so it gets checkpointed)
    assert ctx.messages is cm.messages
    assert [m.content for m in ctx.messages] == ["a"]


def test_model_property_fallback():
    assert ContextManager().model == "gpt-4"  # generic default
    assert ContextManager(model="explicit").model == "explicit"
    assert ContextManager(llm=FakeLLM(model="from-llm")).model == "from-llm"


def test_token_state_returns_snapshot():
    cm = ContextManager(model="gpt-4")
    cm.add(text_msg("hello world"))
    state = cm.token_state()
    assert state.model == "gpt-4"
    assert state.token_count > 0


# ---------------------------------------------------------------------------
# manage_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_history_empty_returns_false():
    cm = ContextManager(model="gpt-4")
    assert await cm.manage_history() is False


@pytest.mark.asyncio
async def test_manage_history_microcompact_only():
    # No llm → only the cheap pass runs. 4 Read pairs over trigger=2 fold.
    ctx = LLMCallContext()
    cm = ContextManager(ctx, config=MICRO_CFG, model="gpt-4")
    cm.add_batch(make_pairs(4))
    changed = await cm.manage_history()
    assert changed is True
    cleared = [m for m in ctx.messages if m.content == "[Old tool result content cleared]"]
    assert len(cleared) == 3


@pytest.mark.asyncio
async def test_manage_history_no_trigger_returns_false():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    cm.add_batch(make_pairs(2))  # below microcompact trigger, no autocompact llm
    assert await cm.manage_history() is False


@pytest.mark.asyncio
async def test_manage_history_runs_autocompact(force_autocompact_threshold):
    ctx = LLMCallContext()
    cfg = ContextManagerConfig(
        enable_microcompact=False,
        keep_tail_messages=1,
        keep_tail_tokens=1,
    )
    llm = FakeLLM(summary="<summary>compacted</summary>")
    cm = ContextManager(ctx, llm=llm, config=cfg, model="m")
    cm.add_batch([text_msg(f"turn {i} content here") for i in range(6)])
    changed = await cm.manage_history()
    assert changed is True
    # history replaced by [summary] + tail, swapped into the backing context
    assert len(ctx.messages) == 2
    assert "compacted" in ctx.messages[0].content


@pytest.mark.asyncio
async def test_manage_history_threads_failure_counter(force_autocompact_threshold):
    cfg = ContextManagerConfig(
        enable_microcompact=False,
        keep_tail_messages=1,
        keep_tail_tokens=1,
        max_consecutive_failures=5,
    )
    llm = FakeLLM(raise_exc=RuntimeError("nope"))
    cm = ContextManager(llm=llm, config=cfg, model="m")
    cm.add_batch([text_msg(f"turn {i}") for i in range(6)])
    await cm.manage_history()
    assert cm._consecutive_failures == 1
    await cm.manage_history()
    assert cm._consecutive_failures == 2


# ---------------------------------------------------------------------------
# prepare_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_request_appends_prompt_without_storing():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    cm.add(text_msg("history"))
    req = await cm.prepare_request("the new prompt")
    assert [m.content for m in req] == ["history", "the new prompt"]
    assert isinstance(req[-1], UserMessage)
    # the prompt is in the request but NOT in the stored history
    assert cm.count() == 1


@pytest.mark.asyncio
async def test_prepare_request_accepts_message_object():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    cm.add(text_msg("history"))
    prompt_msg = UserMessage(content="prebuilt")
    req = await cm.prepare_request(prompt_msg)
    assert req[-1] is prompt_msg


@pytest.mark.asyncio
async def test_prepare_request_none_prompt_returns_history_copy():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    cm.add(text_msg("history"))
    req = await cm.prepare_request(None)
    assert [m.content for m in req] == ["history"]
    # a fresh list — mutating it does not touch the store
    req.append(text_msg("scratch"))
    assert cm.count() == 1


@pytest.mark.asyncio
async def test_prepare_request_manage_false_skips_compaction():
    ctx = LLMCallContext()
    cm = ContextManager(ctx, config=MICRO_CFG, model="gpt-4")
    cm.add_batch(make_pairs(4))  # would fold if management ran
    req = await cm.prepare_request("prompt", manage=False)
    assert all(m.content != "[Old tool result content cleared]" for m in ctx.messages)
    assert req[-1].content == "prompt"
