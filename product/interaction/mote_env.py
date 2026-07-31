#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MoteEnv — the human-channel face of the agent control plane.

``MoteEnv`` is the concrete environment that roles run inside. It is the
``isinstance`` target gated by :meth:`Role.ask_user` / :meth:`Role.reply_to_user`
(``role.py``): those methods only talk to the human channel when their ``env`` is
a :class:`MoteEnv`. Everything else (membership, routing, scheduling, residency)
is inherited unchanged from :class:`AgentEnvironment`.

The two human-channel methods:
  * ``ask_user`` blocks on the console/human-input hook and returns the reply,
  * ``reply_to_user`` acknowledges a one-way message back to the user.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from mote.contracts.interaction import ApprovalRequest
from mote.orchestration.agents.environment_facade import AgentEnvironment


class MoteEnv(AgentEnvironment):
    """The control-plane environment with a human input/output channel."""

    def __init__(
        self,
        *args,
        human_input: Callable[[str], Awaitable[str]] | None = None,
        approval_prompt: Callable[[ApprovalRequest], str] | None = None,
        approval_parser: Callable[[str], str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._human_input = human_input
        self._approval_prompt = approval_prompt
        self._approval_parser = approval_parser

    async def _read_human(self, prompt: str) -> str:
        if self._human_input is None:
            raise RuntimeError("MoteEnv requires an injected human input channel")
        return await self._human_input(prompt)

    async def ask_user(self, question: str, sent_from: Optional[Any] = None) -> str:
        """Block on the human-input channel and return the user's response.

        ``sent_from`` is accepted for parity with the role call site (and remote
        overrides) but is not used by the default console implementation.
        """
        rsp = await self._read_human(question)
        return "Human response: " + rsp

    async def reply_to_user(self, content: str, sent_from: Optional[Any] = None) -> str:
        """Acknowledge a one-way reply delivered to the user."""
        return (
            "SUCCESS, human has received your reply. Refrain from resending duplicate "
            "messages. If you no longer need to take action, use the command 'end' to stop."
        )

    async def request_approval(self, request: Any, sent_from: Optional[Any] = None) -> str:
        """Approve a gated action over the console (no structured selector).

        The bare-console fallback: render the structured request to a text
        prompt, block on ``get_human_input``, and parse the typed reply back to
        an :data:`~mote.contracts.interaction.ApprovalChoice`. The rich
        CLI front-end (``PortHumanChannel``) drives a localized port selector
        instead; this path is the developer/headless console.
        """
        if self._approval_prompt is None or self._approval_parser is None:
            raise RuntimeError("MoteEnv requires injected approval presentation")
        rsp = await self._read_human(self._approval_prompt(request))
        return self._approval_parser(rsp)

    def __repr__(self) -> str:
        return "MoteEnv()"


__all__ = ["MoteEnv"]
