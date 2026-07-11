#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TextualPort`` — Future-based ``read_turn`` + callback-resolved modal asks (§B).

The full-screen host carries no raw-stdin machinery: ``read_turn`` awaits one
``asyncio.Future`` resolved by ``feed_turn`` (or ``request_exit`` → ``None``), and
``ask`` / ``decide_approval`` push a modal whose dismissal callback resolves a
Future. These tests drive that contract with a fake app.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from mote.cli.contracts.view.events import ApprovalDecision
from mote.cli.io.textual_io import TextualPort


def _q(question, header, options, multiSelect=False):
    return {
        "question": question,
        "header": header,
        "options": [{"label": l, "description": d} for l, d in options],
        "multiSelect": multiSelect,
    }


def _questions(*qs):
    from mote.common.schema import AskUserQuestionInput

    return AskUserQuestionInput.model_validate({"questions": list(qs)})


class _FakeApp:
    """Fake app: ``push_screen`` dismisses synchronously with a preset result."""

    def __init__(self, dismiss_with=None) -> None:
        self.dismiss_with = dismiss_with
        self.pushed: list = []
        self.idle_calls = 0
        self.staged: list = []

    def push_screen(self, screen, callback) -> None:
        self.pushed.append(screen)
        callback(self.dismiss_with)

    def set_idle(self) -> None:
        self.idle_calls += 1

    def stage_prompt(self, text: str) -> None:
        self.staged.append(text)


@pytest.mark.asyncio
async def test_read_turn_resolved_by_feed_turn():
    port = TextualPort(_FakeApp())
    task = asyncio.ensure_future(port.read_turn())
    await asyncio.sleep(0)  # let read_turn create + await its future
    assert port.is_waiting_for_turn() is True
    port.feed_turn("hello")
    assert await task == "hello"
    assert port.is_waiting_for_turn() is False


@pytest.mark.asyncio
async def test_request_exit_resolves_to_none():
    port = TextualPort(_FakeApp())
    task = asyncio.ensure_future(port.read_turn())
    await asyncio.sleep(0)
    port.request_exit()
    assert await task is None
    assert port.should_exit is True


@pytest.mark.asyncio
async def test_read_turn_returns_none_after_exit():
    port = TextualPort(_FakeApp())
    port.request_exit()
    assert await port.read_turn() is None


@pytest.mark.asyncio
async def test_read_turn_notifies_app_idle():
    app = _FakeApp()
    port = TextualPort(app)
    task = asyncio.ensure_future(port.read_turn())
    await asyncio.sleep(0)
    port.feed_turn("x")
    await task
    assert app.idle_calls == 1


@pytest.mark.asyncio
async def test_ask_questions_returns_structured_selection():
    # The QuestionScreen dismisses a structured ``(selected, free_text)`` tuple;
    # ask_questions pairs it back to the question verbatim.
    port = TextualPort(_FakeApp(dismiss_with=(["Blue"], "")))
    result = await port.ask_questions(None, _questions(_q("Pick", "P", [("Red", ""), ("Blue", "")])))
    a = result.answers[0]
    assert a.selected == ["Blue"]
    assert a.free_text == ""


@pytest.mark.asyncio
async def test_ask_questions_free_text_verbatim():
    # A numeric "Other" answer stays free text (no digit→index mapping).
    port = TextualPort(_FakeApp(dismiss_with=([], "42")))
    result = await port.ask_questions(None, _questions(_q("How many?", "Q", [("One", ""), ("Two", "")])))
    a = result.answers[0]
    assert a.selected == []
    assert a.free_text == "42"


@pytest.mark.asyncio
async def test_ask_returns_free_text_from_tuple():
    # Plain ``ask`` flattens the structured dismissal back to a single string.
    port = TextualPort(_FakeApp(dismiss_with=([], "chosen")))
    result = await port.ask(None, "pick?")
    assert result == "chosen"


@pytest.mark.asyncio
async def test_ask_non_tuple_result_becomes_empty():
    port = TextualPort(_FakeApp(dismiss_with=None))
    assert await port.ask(None, "pick?") == ""


@pytest.mark.asyncio
async def test_decide_approval_returns_decision():
    decision = ApprovalDecision(approval_id="ap-1", outcome="accept")
    port = TextualPort(_FakeApp(dismiss_with=decision))
    request = type("Req", (), {"approval_id": "ap-1"})()
    result = await port.decide_approval(None, request)
    assert result is decision


@pytest.mark.asyncio
async def test_decide_approval_fallback_rejects():
    port = TextualPort(_FakeApp(dismiss_with=None))
    request = type("Req", (), {"approval_id": "ap-9"})()
    result = await port.decide_approval(None, request)
    assert isinstance(result, ApprovalDecision)
    assert result.approval_id == "ap-9"
    assert result.outcome == "reject"


def test_signal_interrupt_invokes_hook():
    port = TextualPort(_FakeApp())
    fired = []
    port._on_interrupt = lambda: fired.append(True)
    port.signal_interrupt()
    assert fired == [True]


def test_submit_steer_forwards_nonblank_text():
    port = TextualPort(_FakeApp())
    seen = []
    port._on_steer = lambda text: seen.append(text)
    port.submit_steer(None, "   ")  # blank → ignored
    port.submit_steer(None, "steer me")
    assert seen == ["steer me"]


def test_stage_restore_prefills_prompt():
    app = _FakeApp()
    port = TextualPort(app)
    port.stage_restore("resume this")
    assert app.staged == ["resume this"]
