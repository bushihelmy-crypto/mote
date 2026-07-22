#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.session.subscribers.TitleSubscriber`.

The subscriber fires a single cheap auxiliary-model call on the first live
:class:`UserPromptSubmitEvent` and appends a :class:`MetaUpdateEvent` carrying
the generated title. Covers: the happy path (title appended), the once-only
latch (a second prompt does not re-fire), the resume guard (an already-titled
log never re-generates), the ``enabled`` gate, empty/blank prompt skips, an
empty/blank model reply appending nothing, and generator failure being
swallowed. The model call is faked so nothing touches the network.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from mote.common.events import UserPromptSubmitEvent
from mote.common.events.types import TurnEndEvent
from mote.session.events import MetaUpdateEvent, parse_event
from mote.session.log import SessionLog
from mote.session.subscribers import TitleSubscriber


def _log(tmp_path) -> SessionLog:
    return SessionLog("sess-1", base_dir=str(tmp_path))


def _titles(log: SessionLog) -> list[str]:
    """Every non-empty title appended to the log, in order."""
    out: list[str] = []
    for record in log.iter_raw():
        event = parse_event(record)
        if isinstance(event, MetaUpdateEvent) and event.title:
            out.append(event.title)
    return out


def _make_gen(reply: Optional[str] = "My Title", *, calls: Optional[list] = None):
    """A fake ``prompt -> title`` coroutine recording each prompt it sees."""

    async def _gen(prompt: str) -> Optional[str]:
        if calls is not None:
            calls.append(prompt)
        return reply

    return _gen


async def _dispatch(sub: TitleSubscriber, event) -> None:
    """Run handle then await the fire-and-forget task it spawned (if any)."""
    await sub.handle(event)
    if sub._task is not None:
        await sub._task


def test_first_prompt_generates_and_appends_title(tmp_path):
    log = _log(tmp_path)
    calls: list[str] = []
    sub = TitleSubscriber(log, _make_gen("Fix the parser", calls=calls))
    asyncio.run(_dispatch(sub, UserPromptSubmitEvent(prompt="please fix the parser bug")))
    assert calls == ["please fix the parser bug"]
    assert _titles(log) == ["Fix the parser"]


def test_only_fires_once_on_second_prompt(tmp_path):
    log = _log(tmp_path)
    calls: list[str] = []
    sub = TitleSubscriber(log, _make_gen("T", calls=calls))

    async def _run():
        await _dispatch(sub, UserPromptSubmitEvent(prompt="first"))
        await _dispatch(sub, UserPromptSubmitEvent(prompt="second"))

    asyncio.run(_run())
    assert calls == ["first"]  # second prompt did not re-fire
    assert _titles(log) == ["T"]


def test_resume_with_existing_title_never_regenerates(tmp_path):
    log = _log(tmp_path)
    log.append(MetaUpdateEvent(title="Existing Title"))
    calls: list[str] = []
    # A fresh subscriber over the same (already-titled) log seeds _done at build.
    sub = TitleSubscriber(log, _make_gen("New Title", calls=calls))
    assert sub._done is True
    asyncio.run(_dispatch(sub, UserPromptSubmitEvent(prompt="another prompt")))
    assert calls == []  # the generator was never consulted
    assert _titles(log) == ["Existing Title"]


def test_disabled_does_nothing(tmp_path):
    log = _log(tmp_path)
    calls: list[str] = []
    sub = TitleSubscriber(log, _make_gen(calls=calls), enabled=False)
    asyncio.run(_dispatch(sub, UserPromptSubmitEvent(prompt="hello")))
    assert calls == []
    assert _titles(log) == []


def test_blank_prompt_skipped(tmp_path):
    log = _log(tmp_path)
    calls: list[str] = []
    sub = TitleSubscriber(log, _make_gen(calls=calls))
    asyncio.run(_dispatch(sub, UserPromptSubmitEvent(prompt="   ")))
    assert calls == []
    assert sub._done is False  # a blank prompt does not burn the once-only latch
    assert _titles(log) == []


def test_non_prompt_event_ignored(tmp_path):
    log = _log(tmp_path)
    calls: list[str] = []
    sub = TitleSubscriber(log, _make_gen(calls=calls))
    asyncio.run(_dispatch(sub, TurnEndEvent(turn_id="t1")))
    assert calls == []
    assert _titles(log) == []


def test_empty_reply_appends_nothing(tmp_path):
    log = _log(tmp_path)
    sub = TitleSubscriber(log, _make_gen("   "))  # model returns only whitespace
    asyncio.run(_dispatch(sub, UserPromptSubmitEvent(prompt="do a thing")))
    assert _titles(log) == []


def test_generator_failure_is_swallowed(tmp_path):
    log = _log(tmp_path)

    async def _boom(prompt: str) -> Optional[str]:
        raise RuntimeError("model down")

    sub = TitleSubscriber(log, _boom)
    # Must not raise; nothing appended.
    asyncio.run(_dispatch(sub, UserPromptSubmitEvent(prompt="trigger")))
    assert _titles(log) == []


def test_last_prompt_preview_recorded(tmp_path):
    log = _log(tmp_path)
    sub = TitleSubscriber(log, _make_gen("Title"))
    asyncio.run(_dispatch(sub, UserPromptSubmitEvent(prompt="a detailed request here")))
    metas = [parse_event(r) for r in log.iter_raw()]
    meta = next(m for m in metas if isinstance(m, MetaUpdateEvent) and m.title)
    assert meta.last_prompt == "a detailed request here"
