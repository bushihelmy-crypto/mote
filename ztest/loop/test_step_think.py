#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ReActLoop._step_think`` — the single think primitive.

Returns ``False`` immediately when the shared ``active`` signal is off (the
``End`` tool / ask_human "stop" path); otherwise it asks the context provider to
``prepare()`` the request, lazily ``resolve_llm()`` and hands everything to
``think_engine.start()``, returning ``True``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_step_think_returns_false_when_inactive(make_loop):
    b = make_loop(active=False)
    b.loop._ctx = b.ctx

    assert await b.loop._step_think() is False
    # Nothing was prepared / started.
    assert b.provider.prepare_calls == 0
    assert b.think_engine.start_calls == []


async def test_step_think_prepares_and_starts(make_loop):
    b = make_loop(active=True)
    b.loop._ctx = b.ctx

    assert await b.loop._step_think() is True
    assert b.provider.prepare_calls == 1
    # The resolved LLM was driven off the prepared request's messages.
    assert b.provider.resolve_calls == [b.provider._think_request.req]

    call = b.think_engine.start_calls[0]
    tr = b.provider._think_request
    assert call["req"] is tr.req
    assert call["system_prompt"] == tr.system_prompt
    assert call["state_data"] == tr.state_data
    assert call["tool_specs"] == tr.tool_specs
    assert call["llm"] is b.provider.llm
