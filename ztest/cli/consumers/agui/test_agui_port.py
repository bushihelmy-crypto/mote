#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`AguiPort` — the AG-UI input half (Phase 2 + Phase 3 HITL).

AG-UI drives exactly one turn per request, so ``read_turn`` yields the injected
message once then ``None``.

The interactive round-trips have two regimes:

* **No back-channel** (Phase-2 read-only stream: no ``sink``/``broker``) — the
  round-trips return *safe non-blocking defaults* (empty answer / reject) so a
  turn never blocks on a human who can't reply.
* **Wired back-channel** (Phase 3) — the port emits a prompt frame down the SSE
  ``sink`` and blocks on a :class:`PromptBroker` future the separate ``POST
  /respond`` handler resolves. These tests drive that loop with a fake list
  ``sink`` + a real broker, resolving the minted prompt from the emitted frame.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from mote.contracts.interaction import AskUserQuestionInput
from mote.product.cli.consumers._wire import agui
from mote.product.cli.consumers.agui.port import AguiPort
from mote.product.cli.contracts.view.events import ApprovalRequested
from mote.product.cli.serving import PromptBroker


# ── helpers ────────────────────────────────────────────────────────────────
def _wired_port(*, timeout_s: float = 5.0):
    """An :class:`AguiPort` over a fake list ``sink`` + a real broker.

    Returns ``(port, frames, broker)`` — ``frames`` collects every emitted wire
    dict so a test can read the minted id back out of the prompt frame.
    """
    frames: List[Dict[str, Any]] = []

    async def sink(frame: Dict[str, Any]) -> None:
        frames.append(frame)

    broker = PromptBroker()
    port = AguiPort("x", sink=sink, broker=broker, thread_id="t", run_id="r", timeout_s=timeout_s)
    return port, frames, broker


def _prompt_id(frame: Dict[str, Any]) -> str:
    """Pull the minted correlation id out of an emitted approval/question frame."""
    value = frame["value"]
    return value.get("approvalId") or value.get("questionId") or ""


# ── Phase 2: turn boundary + safe defaults (no back-channel) ────────────────
@pytest.mark.asyncio
async def test_read_turn_yields_message_once_then_none():
    port = AguiPort("hello")
    assert await port.read_turn() == "hello"
    assert await port.read_turn() is None  # turn is over; SSE stream closes


@pytest.mark.asyncio
async def test_ask_returns_empty_without_blocking():
    port = AguiPort("x")
    assert await port.ask(None, "your name?") == ""


@pytest.mark.asyncio
async def test_ask_questions_returns_empty_answers():
    port = AguiPort("x")
    answers = await port.ask_questions(None, object())  # type: ignore[arg-type]
    assert answers.answers == []


@pytest.mark.asyncio
async def test_decide_approval_rejects_without_backchannel():
    # No sink/broker → fail-safe reject; the port mints no id, so approval_id is
    # empty (Phase 3 semantics — the old ViewEvent-id passthrough is gone).
    port = AguiPort("x")
    req = ApprovalRequested(tool_name="Bash", action="rm -rf /", approval_id="ap-1")
    decision = await port.decide_approval(None, req)
    assert decision.approval_id == ""
    assert decision.outcome == "reject"  # fail-safe: no back-channel → deny


def test_interrupt_and_steer_are_noops():
    port = AguiPort("x")
    # These must not raise — Phase 2 has no mid-stream steer/interrupt control.
    assert port.signal_interrupt(None) is None
    assert port.submit_steer(None, "later") is None


# ── Phase 3: HITL round-trips over a wired sink + broker ────────────────────
@pytest.mark.asyncio
async def test_decide_approval_accept_round_trip():
    port, frames, broker = _wired_port()
    req = ApprovalRequested(tool_name="Bash", action="rm -rf /", approval_id="ap-1")

    task = asyncio.create_task(port.decide_approval(None, req))
    await asyncio.sleep(0)  # let the port emit + register its future
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == agui.CUSTOM
    assert frame["name"] == "approval"
    pid = _prompt_id(frame)
    assert pid  # a fresh minted correlation id, not the ViewEvent id
    assert pid != "ap-1"

    assert broker.resolve(pid, {"promptId": pid, "outcome": "accept"}) is True
    decision = await task
    assert decision.approval_id == pid
    assert decision.outcome == "accept"


@pytest.mark.asyncio
async def test_decide_approval_reject_round_trip():
    port, frames, broker = _wired_port()
    req = ApprovalRequested(tool_name="Bash", action="rm -rf /", approval_id="ap-1")

    task = asyncio.create_task(port.decide_approval(None, req))
    await asyncio.sleep(0)
    pid = _prompt_id(frames[0])
    assert broker.resolve(pid, {"promptId": pid, "outcome": "reject"}) is True
    decision = await task
    assert decision.outcome == "reject"


@pytest.mark.asyncio
async def test_decide_approval_edited_args_passthrough():
    port, frames, broker = _wired_port()
    req = ApprovalRequested(tool_name="Bash", action="echo hi", approval_id="ap-1")

    task = asyncio.create_task(port.decide_approval(None, req))
    await asyncio.sleep(0)
    pid = _prompt_id(frames[0])
    edited = {"command": "echo edited"}
    broker.resolve(pid, {"promptId": pid, "outcome": "accept", "editedArgs": edited})
    decision = await task
    assert decision.outcome == "accept"
    assert decision.edited_args == edited


@pytest.mark.asyncio
async def test_decide_approval_always_allow_round_trip():
    port, frames, broker = _wired_port()
    req = ApprovalRequested(tool_name="Bash", action="ls", approval_id="ap-1")

    task = asyncio.create_task(port.decide_approval(None, req))
    await asyncio.sleep(0)
    pid = _prompt_id(frames[0])
    broker.resolve(pid, {"promptId": pid, "outcome": "always_allow"})
    decision = await task
    assert decision.outcome == "always_allow"


@pytest.mark.asyncio
async def test_decide_approval_garbled_reply_rejects():
    # A reply with an unknown outcome falls back to reject (fail-safe).
    port, frames, broker = _wired_port()
    req = ApprovalRequested(tool_name="Bash", action="ls", approval_id="ap-1")

    task = asyncio.create_task(port.decide_approval(None, req))
    await asyncio.sleep(0)
    pid = _prompt_id(frames[0])
    broker.resolve(pid, {"promptId": pid, "outcome": "maybe"})
    decision = await task
    assert decision.outcome == "reject"


@pytest.mark.asyncio
async def test_ask_round_trip_returns_reply_text():
    port, frames, broker = _wired_port()

    task = asyncio.create_task(port.ask(None, "your name?"))
    await asyncio.sleep(0)
    frame = frames[0]
    assert frame["type"] == agui.CUSTOM
    assert frame["name"] == "question"
    pid = _prompt_id(frame)
    broker.resolve(pid, {"promptId": pid, "answer": "Ada"})
    assert await task == "Ada"


@pytest.mark.asyncio
async def test_ask_questions_round_trip_structured_answers():
    port, frames, broker = _wired_port()
    questions = AskUserQuestionInput(
        questions=[
            {
                "question": "Pick a color",
                "header": "Color",
                "options": [
                    {"label": "red", "description": "warm"},
                    {"label": "blue", "description": "cool"},
                ],
            }
        ]
    )

    task = asyncio.create_task(port.ask_questions(None, questions))
    await asyncio.sleep(0)
    frame = frames[0]
    assert frame["name"] == "question"
    assert frame["value"].get("structured") is not None  # rich payload for the frontend
    pid = _prompt_id(frame)
    broker.resolve(
        pid,
        {
            "promptId": pid,
            "answers": [{"header": "Color", "question": "Pick a color", "selected": ["red"]}],
        },
    )
    answers = await task
    assert len(answers.answers) == 1
    assert answers.answers[0].header == "Color"
    assert answers.answers[0].selected == ["red"]


@pytest.mark.asyncio
async def test_prompt_timeout_falls_back_to_safe_default():
    # A wired frontend that never answers must not wedge the turn — the bounded
    # timeout fires and the port returns its safe default (reject).
    port, frames, broker = _wired_port(timeout_s=0.05)
    req = ApprovalRequested(tool_name="Bash", action="ls", approval_id="ap-1")
    decision = await port.decide_approval(None, req)
    assert decision.outcome == "reject"
    # the timed-out prompt was discarded from the broker
    assert broker.pending_ids == []


@pytest.mark.asyncio
async def test_closed_port_short_circuits_to_default():
    port, frames, broker = _wired_port()
    await port.aclose()
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "reject"
    assert frames == []  # nothing emitted once closed
