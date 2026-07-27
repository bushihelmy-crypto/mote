#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`TimestampContextSource` — per-turn wall-clock feed.

The current local time moved off the request tail's ``current_state`` line into
the structured reminder envelope. These tests assert it always emits a block
(the time is relevant every turn), the block carries the rendered time, and it
is a proper ephemeral (request-only) source.
"""
from __future__ import annotations

import asyncio

from mote.contracts.ports import EphemeralContextSource
from mote.runtime.context.turn_context import TimestampContextSource


def run(coro):
    return asyncio.run(coro)


def test_is_ephemeral_context_source():
    src = TimestampContextSource()
    assert isinstance(src, EphemeralContextSource)
    assert src.save_to_context is False


def test_renders_current_time_block():
    out = run(TimestampContextSource().render())
    assert out is not None
    assert "Current local time is" in out
