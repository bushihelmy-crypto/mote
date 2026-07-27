#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`AcpPort` — the ACP input half (turn boundary + HITL).

ACP is a STATEFUL bidirectional link: the agent sends the client a
``session/request_permission`` request and awaits its reply inline on the same
connection. So :meth:`decide_approval` blocks directly on an injected
``request`` callable (no cross-request broker, unlike AG-UI). These tests drive
that round-trip with a fake ``request`` recording the outbound params and
returning a canned ``RequestPermissionResponse``.

ACP has no native free-text / structured-question client method, so ``ask`` /
``ask_questions`` return safe non-blocking defaults; the turn boundary yields
the prompt text once then ``None``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from mote.product.cli.consumers._wire import acp
from mote.product.cli.consumers.acp.port import AcpPort
from mote.product.cli.contracts.view.events import ApprovalRequested


def _reply(option_id: Optional[str], *, cancelled: bool = False) -> Dict[str, Any]:
    """A canned ``RequestPermissionResponse`` outcome envelope."""
    if cancelled:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _wired_port(reply: Any, *, timeout_s: float = 5.0):
    """An :class:`AcpPort` over a fake ``request`` returning *reply*.

    Returns ``(port, calls)`` — ``calls`` collects every ``(method, params)``
    tuple the port sent so a test can inspect the outbound permission request.
    """
    calls: List[tuple] = []

    async def request(method: str, params: Dict[str, Any]) -> Any:
        calls.append((method, params))
        if callable(reply):
            return await reply()
        return reply

    port = AcpPort("hi", session_id="sess-1", request=request, timeout_s=timeout_s)
    return port, calls


# ── turn boundary ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_read_turn_yields_text_once_then_none():
    port = AcpPort("hello", session_id="s")
    assert await port.read_turn() == "hello"
    assert await port.read_turn() is None


# ── ACP has no free-text / structured channel → safe defaults ────────────────
@pytest.mark.asyncio
async def test_ask_returns_empty():
    port = AcpPort("x", session_id="s")
    assert await port.ask(None, "name?") == ""


@pytest.mark.asyncio
async def test_ask_questions_returns_empty_answers():
    port = AcpPort("x", session_id="s")
    answers = await port.ask_questions(None, object())  # type: ignore[arg-type]
    assert answers.answers == []


# ── permission round-trip (the one native ACP interactive path) ──────────────
@pytest.mark.asyncio
async def test_decide_approval_allow_once_maps_to_accept():
    port, calls = _wired_port(_reply(acp.PERM_ALLOW_ONCE))
    req = ApprovalRequested(tool_name="Bash", action="ls", approval_id="ap-1")
    decision = await port.decide_approval(None, req)
    assert decision.outcome == "accept"
    # the outbound request is session/request_permission with the four options
    method, params = calls[0]
    assert method == "session/request_permission"
    assert params["sessionId"] == "sess-1"
    assert [o["optionId"] for o in params["options"]] == [
        acp.PERM_ALLOW_ONCE,
        acp.PERM_ALLOW_ALWAYS,
        acp.PERM_REJECT_ONCE,
        acp.PERM_REJECT_ALWAYS,
    ]
    assert params["toolCall"]["toolCallId"]  # correlated to a tool call id


@pytest.mark.asyncio
async def test_decide_approval_allow_always_maps_to_always_allow():
    port, _ = _wired_port(_reply(acp.PERM_ALLOW_ALWAYS))
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "always_allow"


@pytest.mark.asyncio
async def test_decide_approval_reject_once_maps_to_reject():
    port, _ = _wired_port(_reply(acp.PERM_REJECT_ONCE))
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "reject"


@pytest.mark.asyncio
async def test_decide_approval_reject_always_maps_to_always_deny():
    port, _ = _wired_port(_reply(acp.PERM_REJECT_ALWAYS))
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "always_deny"


@pytest.mark.asyncio
async def test_decide_approval_cancelled_rejects():
    port, _ = _wired_port(_reply(None, cancelled=True))
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "reject"  # fail-safe: cancelled → no


@pytest.mark.asyncio
async def test_decide_approval_unknown_option_rejects():
    port, _ = _wired_port(_reply("garbage_option"))
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "reject"


@pytest.mark.asyncio
async def test_decide_approval_no_request_callable_rejects():
    # No wired request sender → fail-safe reject (never block a turn).
    port = AcpPort("x", session_id="s", request=None)
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "reject"


@pytest.mark.asyncio
async def test_decide_approval_timeout_rejects():
    async def never():
        await asyncio.sleep(10)

    port, _ = _wired_port(never, timeout_s=0.05)
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "reject"


@pytest.mark.asyncio
async def test_closed_port_short_circuits_to_reject():
    port, calls = _wired_port(_reply(acp.PERM_ALLOW_ONCE))
    await port.aclose()
    decision = await port.decide_approval(None, ApprovalRequested(tool_name="Bash", action="ls"))
    assert decision.outcome == "reject"
    assert calls == []  # nothing sent once closed


# ── control affordances are inert (server owns the run task) ─────────────────
def test_interrupt_and_steer_are_noops():
    port = AcpPort("x", session_id="s")
    assert port.signal_interrupt(None) is None
    assert port.submit_steer(None, "later") is None
