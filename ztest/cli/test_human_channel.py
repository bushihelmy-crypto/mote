#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`PortHumanChannel` — the env adapter routing human prompts.

The load-bearing contract here is *serialization*: one console has one reader,
so concurrent human prompts must queue rather than interleave. This matters once
a tool can fan out parallel work that each raises a prompt (e.g. ``run_graph``'s
map / AND-join branches dispatching approval-gated or AskUserQuestion tools at
once) — the port's single-reader guard coordinates only with the main-loop
reader, not two prompts against each other, so the channel-level lock is what
keeps their output from interleaving and their parked-waiter slots from clobbering.
"""
from __future__ import annotations

import asyncio

from mote.product.cli.io.human_channel import PortHumanChannel


class _SlowPort:
    """Port whose calls are deliberately non-atomic (yield mid-call).

    Records a ('start', tag) before yielding and ('end', tag) after, so an
    interleaving is directly visible in the log: serialized calls produce
    strictly paired start/end runs; interleaved calls do not.
    """

    def __init__(self):
        self.log: list[tuple] = []

    async def _round_trip(self, tag):
        self.log.append(("start", tag))
        await asyncio.sleep(0)  # hand control back mid-prompt
        await asyncio.sleep(0)
        self.log.append(("end", tag))

    async def ask(self, ctx, question, options=None, multi=False):
        await self._round_trip(question)
        return f"ans:{question}"

    async def ask_questions(self, ctx, questions):
        await self._round_trip("Q")
        return questions

    async def decide_approval(self, ctx, request):
        await self._round_trip("approve")

        class _D:
            outcome = "accept"

        return _D()


def _is_serialized(log) -> bool:
    """Every start is immediately followed by its own matching end."""
    if len(log) % 2:
        return False
    return all(log[i][0] == "start" and log[i + 1] == ("end", log[i][1]) for i in range(0, len(log), 2))


def test_concurrent_ask_user_serialized():
    port = _SlowPort()
    ch = PortHumanChannel(port)

    async def go():
        await asyncio.gather(ch.ask_user("A"), ch.ask_user("B"), ch.ask_user("C"))

    asyncio.run(go())
    assert _is_serialized(port.log), port.log
    assert len(port.log) == 6


def test_mixed_prompt_kinds_serialized():
    # ask_user, ask_user_question and request_approval all share one lock, so a
    # burst mixing all three still never interleaves.
    port = _SlowPort()
    ch = PortHumanChannel(port)

    async def go():
        await asyncio.gather(
            ch.ask_user("A"),
            ch.ask_user_question(["q1"]),
            ch.request_approval(object()),
        )

    asyncio.run(go())
    assert _is_serialized(port.log), port.log
    assert len(port.log) == 6
