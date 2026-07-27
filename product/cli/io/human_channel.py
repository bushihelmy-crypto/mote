#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``PortHumanChannel`` — env adapter routing ``Role.ask_user`` to an InputPort.

``Role.ask_user`` (the capability behind the ``AskUserQuestion`` tool) delegates
to ``state.env.ask_user(...)``. This is the §8 successor of the old
``_ConsoleHumanChannel``: instead of hard-wiring the REPL console, it routes to
any :class:`~mote.product.cli.contracts.interface.ports.InputPort` (terminal / Web / IM share the same
``ask`` contract, §2.5), so the human channel is uniform across platforms.

Approvals ride the same uniformity: ``request_approval`` receives a structured
:class:`~mote.contracts.permissions.ApprovalRequest` from the engine
and drives the port's ``decide_approval`` selector directly, mapping the
returned :class:`ApprovalDecision` outcome back to the engine's
:data:`~mote.contracts.permissions.ApprovalChoice` vocabulary. No prose
is assembled or parsed — the localized display wording lives entirely in the
port/consumer layer (under i18n).

The single-agent driver has no multi-role environment, so address registration
and message publishing the Role might call on its env are inert no-ops; an empty
``desc`` / ``roles`` short-circuits the provider's "other roles" / "team info"
prefixes entirely.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mote.contracts.handoff import HandoffStatus, HumanHandoffOutcome
from mote.contracts.interaction import AskUserQuestionAnswer, AskUserQuestionAnswers

# Map a structured ApprovalDecision.outcome to the engine's ApprovalChoice.
# ``always_deny`` currently collapses to a plain deny (no persistent deny rule
# yet); when that lands it maps to its own choice.
_OUTCOME_TO_CHOICE = {
    "accept": "allow_once",
    "always_allow": "allow_session",
    "reject": "deny",
    "always_deny": "deny",
}


class PortHumanChannel:
    """Minimal env adapter: ``ask_user`` → ``port.ask``; everything else inert."""

    # The provider reads ``env.desc`` / ``env.role_names()`` / ``env.roles`` when
    # building the role prefix + team info; all three are inert for a single
    # agent (empty desc/roles short-circuits those prefixes).
    desc: str = ""
    roles: dict = {}

    def __init__(self, port: Any, *, ctx: Any = None):
        self._port = port  # any InputPort (has ``async ask(ctx, question) -> str``)
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

    def role_names(self) -> list:
        return []

    async def ask_user(self, question: str, sent_from: Any = None) -> str:
        async with self._prompt_lock:
            return await self._port.ask(self._ctx, question)

    async def ask_user_question(self, questions: Any, sent_from: Any = None) -> Any:
        """Route structured multiple-choice questions to the port's ``ask_questions``.

        The structured counterpart of ``ask_user`` behind the ``AskUserQuestion``
        tool: a full round-trip (down as display, back up as structured answers)
        with zero text parsing. Ports that predate ``ask_questions`` degrade
        per-question through the plain ``ask``, still building structured answers.
        """
        async with self._prompt_lock:
            if hasattr(self._port, "ask_questions"):
                return await self._port.ask_questions(self._ctx, questions)
            return await self._degrade_ask_questions(questions)

    async def _degrade_ask_questions(self, questions: Any) -> Any:
        """Loss-free per-question fallback for ports without ``ask_questions``.

        Builds *structured* answers, so — unlike the old text round-trip — there
        is no ``\\n\\n`` block splitting and no line-position pairing: bug #1 / #2
        cannot recur even on this path.
        """
        out = []
        for q in questions:
            labels = [o.label for o in q.options]
            reply = await self._ask_text(q.question, labels, q.multiSelect)  # -> str
            picks = [r.strip() for r in reply.split(",")] if q.multiSelect else [reply.strip()]
            selected = [p for p in picks if p in labels]
            free = "" if selected == picks else reply.strip()
            out.append(AskUserQuestionAnswer(header=q.header, question=q.question, selected=selected, free_text=free))
        return AskUserQuestionAnswers(answers=out)

    async def _ask_text(self, question: str, labels: list, multi: bool) -> str:
        """Call ``port.ask`` with structured options, degrading if unsupported."""
        try:
            return await self._port.ask(self._ctx, question, options=labels, multi=multi)
        except TypeError:
            try:
                return await self._port.ask(self._ctx, question, options=labels)
            except TypeError:
                return await self._port.ask(self._ctx, question)

    async def request_approval(self, request: Any, sent_from: Any = None) -> str:
        """Drive the port's structured approval selector; return an ``ApprovalChoice``.

        The inbound half of the ``request_approval`` capability: the engine hands
        us a language-neutral :class:`ApprovalRequest`, we route it straight to
        the port's ``decide_approval`` (which renders the localized selector and
        returns an :class:`ApprovalDecision`), then map its outcome back to the
        engine's :data:`ApprovalChoice`. A port with no selector fails closed.
        """
        async with self._prompt_lock:
            if not hasattr(self._port, "decide_approval"):
                return "deny"
            decision = await self._port.decide_approval(self._ctx, request)
            outcome = getattr(decision, "outcome", "reject")
            return _OUTCOME_TO_CHOICE.get(outcome, "deny")

    async def open_handoff(self, request: Any, handle: Any, surface: Any = None) -> HumanHandoffOutcome:
        """Open a host-native Runtime surface without using the ask channel."""
        async with self._prompt_lock:
            if not hasattr(self._port, "open_handoff"):
                return HumanHandoffOutcome(status=HandoffStatus.UNAVAILABLE)
            return await self._port.open_handoff(request, handle, surface)

    async def reply_to_user(self, content: str, sent_from: Any = None) -> str:
        return ""

    def set_addresses(self, role: Any, addresses: Any) -> None:  # noqa: D401 — no-op
        pass

    def publish_message(self, msg: Any) -> None:  # noqa: D401 — no-op
        pass


__all__ = ["PortHumanChannel"]
