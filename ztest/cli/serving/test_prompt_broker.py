#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`PromptBroker` — the blocking-prompt ↔ back-channel rendezvous.

A HITL prompt raised while a ``POST /run`` turn streams blocks on a future the
separate ``POST /respond`` handler resolves. The broker is that app-scoped
``prompt_id → Future`` map: :meth:`open` registers + awaits, :meth:`resolve`
delivers, :meth:`cancel_all` fails every pending waiter on shutdown so no turn
hangs. These tests exercise each edge without any transport.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.product.cli.serving import PromptBroker


def test_new_id_is_unique_and_prefixed():
    broker = PromptBroker()
    a = broker.new_id("approval")
    b = broker.new_id("approval")
    assert a != b
    assert a.startswith("approval-")


@pytest.mark.asyncio
async def test_open_then_resolve_delivers_payload():
    broker = PromptBroker()
    pid = broker.new_id("q")
    fut = broker.open(pid)
    assert broker.pending_ids == [pid]

    assert broker.resolve(pid, {"answer": "hi"}) is True
    assert await fut == {"answer": "hi"}
    assert broker.pending_ids == []  # resolved entry is popped


def test_resolve_unknown_id_returns_false():
    broker = PromptBroker()
    assert broker.resolve("nope", {"x": 1}) is False


@pytest.mark.asyncio
async def test_double_resolve_second_is_false():
    broker = PromptBroker()
    pid = broker.new_id("q")
    broker.open(pid)
    assert broker.resolve(pid, {"a": 1}) is True
    assert broker.resolve(pid, {"a": 2}) is False  # already delivered + popped


@pytest.mark.asyncio
async def test_discard_drops_without_resolving():
    broker = PromptBroker()
    pid = broker.new_id("q")
    broker.open(pid)
    broker.discard(pid)
    assert broker.pending_ids == []
    # a subsequent resolve finds nothing pending
    assert broker.resolve(pid, {"a": 1}) is False


@pytest.mark.asyncio
async def test_cancel_all_fails_pending_waiters():
    broker = PromptBroker()
    pid = broker.new_id("q")
    fut = broker.open(pid)
    broker.cancel_all("shutdown")
    assert broker.pending_ids == []
    with pytest.raises(asyncio.CancelledError):
        await fut


@pytest.mark.asyncio
async def test_open_reuses_pending_future():
    # A duplicate open for the same still-pending id returns the SAME future
    # rather than orphaning the original waiter.
    broker = PromptBroker()
    pid = broker.new_id("q")
    fut1 = broker.open(pid)
    fut2 = broker.open(pid)
    assert fut1 is fut2
    broker.resolve(pid, {"a": 1})
    assert await fut1 == {"a": 1}


@pytest.mark.asyncio
async def test_open_after_done_creates_fresh_future():
    broker = PromptBroker()
    pid = broker.new_id("q")
    fut1 = broker.open(pid)
    broker.resolve(pid, {"a": 1})
    await fut1
    # reopening the (now-popped) id yields a fresh, unresolved future
    fut2 = broker.open(pid)
    assert fut2 is not fut1
    assert not fut2.done()
