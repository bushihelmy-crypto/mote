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

from typing import Any, Optional

from mote.common.logs import get_human_input
from mote.environment.base_env import AgentEnvironment


class MoteEnv(AgentEnvironment):
    """The control-plane environment with a human input/output channel."""

    async def ask_user(self, question: str, sent_from: Optional[Any] = None) -> str:
        """Block on the human-input channel and return the user's response.

        ``sent_from`` is accepted for parity with the role call site (and remote
        overrides) but is not used by the default console implementation.
        """
        rsp = await get_human_input(question)
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
        an :data:`~mote.common.schema.permission_types.ApprovalChoice`. The rich
        CLI front-end (``PortHumanChannel``) drives a localized port selector
        instead; this path is the developer/headless console.
        """
        from mote.executor.permission.prompts import parse_approval_response, render_approval_prompt

        rsp = await get_human_input(render_approval_prompt(request))
        return parse_approval_response(rsp)

    def __repr__(self) -> str:
        return "MoteEnv()"


__all__ = ["MoteEnv"]
