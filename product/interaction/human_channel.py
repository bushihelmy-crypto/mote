#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``PortHumanChannel`` — typed adapter routing Role human I/O to an InputPort.

``Role.ask_user`` (the capability behind the ``AskUserQuestion`` tool) delegates
to the turn-scoped human interaction port. This is the §8 successor of the old
``_ConsoleHumanChannel``: instead of hard-wiring the REPL console, it routes to
any :class:`~mote.product.interaction.ports.InputPort` (terminal / Web / IM share the same
``ask`` contract, §2.5), so the human channel is uniform across platforms.

Approvals ride the same uniformity: ``request_approval`` receives a structured
:class:`~mote.contracts.interaction.ApprovalRequest` from the engine
and drives the port's ``decide_approval`` selector directly, mapping the
returned :class:`ApprovalDecision` outcome back to the engine's
:data:`~mote.contracts.interaction.ApprovalChoice` vocabulary. No prose
is assembled or parsed — the localized display wording lives entirely in the
port/consumer layer (under i18n).

The adapter implements only the Role-owned interaction capability. Multi-role
addressing, message publication, and team metadata are intentionally absent.
"""

from __future__ import annotations

import asyncio

from mote.contracts.interaction import ApprovalChoice, ApprovalRequest, AskUserQuestionAnswers, AskUserQuestionInput
from mote.contracts.interaction.handoff import DriverHandoffHandle, HandoffRequest, HumanHandoffOutcome
from mote.contracts.surface import LiveSurfaceSession
from mote.product.interaction.ports import InputPort


class PortHumanChannel:
    """Turn-scoped adapter from Role interaction commands to an input port."""

    def __init__(self, port: InputPort, *, ctx: str | None = None) -> None:
        self._port = port
        self._ctx = ctx
        # One console, one reader: serialize every human prompt so concurrent
        # callers queue instead of interleaving. This matters once a tool can
        # fan out parallel work that each raises a prompt (e.g. ``run_graph``'s
        # map / AND-join branches dispatching approval-gated or AskUserQuestion
        # tools at once) — the port's single-reader guard coordinates only with
        # the main-loop reader, not two prompts against each other, so without
        # this lock their ``_write`` output interleaves and the second clobbers
        # the port's parked-waiter slot. A plain (non-reentrant) lock is safe:
        # each guarded method is one leaf round-trip and never nests another.
        self._prompt_lock = asyncio.Lock()

    async def ask_user(self, question: str, *, sent_from: str = "") -> str:
        del sent_from
        async with self._prompt_lock:
            return await self._port.ask(self._ctx, question)

    async def ask_user_question(
        self, questions: AskUserQuestionInput, *, sent_from: str = ""
    ) -> AskUserQuestionAnswers:
        """Route structured multiple-choice questions to the port's ``ask_questions``.

        The structured counterpart of ``ask_user`` behind the ``AskUserQuestion``
        tool: a full round-trip (down as display, back up as structured answers)
        with zero text parsing. The typed port must implement this capability;
        missing capabilities fail at composition instead of selecting a fallback.
        """
        del sent_from
        async with self._prompt_lock:
            return await self._port.ask_questions(self._ctx, questions)

    async def request_approval(self, request: ApprovalRequest, *, sent_from: str = "") -> ApprovalChoice:
        """Drive the port's structured approval selector; return an ``ApprovalChoice``.

        The inbound half of the ``request_approval`` capability: the engine hands
        us a language-neutral :class:`ApprovalRequest`, we route it straight to
        the port's ``decide_approval`` (which renders the localized selector and
        returns an :class:`ApprovalDecision`), then map its outcome back to the
        engine's :data:`ApprovalChoice`. A port with no selector fails closed.
        """
        del sent_from
        async with self._prompt_lock:
            decision = await self._port.decide_approval(self._ctx, request)
            outcome = decision.outcome
            if outcome == "accept":
                return "allow_once"
            if outcome == "always_allow":
                return "allow_session"
            return "deny"

    async def open_handoff(
        self,
        request: HandoffRequest,
        handle: DriverHandoffHandle,
        surface: LiveSurfaceSession | None = None,
    ) -> HumanHandoffOutcome:
        """Open a host-native Runtime surface without using the ask channel."""
        async with self._prompt_lock:
            return await self._port.open_handoff(request, handle, surface)

    async def reply_to_user(self, content: str, *, sent_from: str = "") -> str:
        del sent_from
        return ""


__all__ = ["PortHumanChannel"]
