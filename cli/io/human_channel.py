#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``PortHumanChannel`` — env adapter routing ``Role.ask_user`` to an InputPort.

``Role.ask_user`` (the capability behind the ``AskUserQuestion`` tool) delegates
to ``state.env.ask_user(...)``. This is the §8 successor of the old
``_ConsoleHumanChannel``: instead of hard-wiring the REPL console, it routes to
any :class:`~mote.cli.contracts.interface.ports.InputPort` (terminal / Web / IM share the same
``ask`` contract, §2.5), so the human channel is uniform across platforms.

The single-agent driver has no multi-role environment, so address registration
and message publishing the Role might call on its env are inert no-ops; an empty
``desc`` / ``roles`` short-circuits the provider's "other roles" / "team info"
prefixes entirely.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# The PermissionEngine renders approval questions with these markers (see
# ``mote/executor/permission/prompts.py``) and then routes the *free-text*
# reply back through ``request_approval`` → ``ask_user``. We intercept those
# prompts here and drive the port's structured ``decide_approval`` selector
# instead of a raw text input, mapping the choice back to a reply the engine's
# ``parse_approval_response`` understands ("yes" / "always" / "no").
_APPROVAL_MARKERS = ("[APPROVAL REQUIRED]", "[SANDBOX ESCALATION]")

# Map a structured ApprovalDecision.outcome to the engine's free-text reply.
_OUTCOME_TO_REPLY = {
    "accept": "yes",
    "always_allow": "always",
    "reject": "no",
    "always_deny": "no",
}


class _ApprovalRequest:
    """Lightweight request shape the ports' ``decide_approval`` reads.

    Mirrors the fields ``TerminalPort`` / ``TextualPort`` pull off the request
    (``action`` / ``risk`` / ``args_preview`` / ``approval_id``) so an
    engine-rendered approval prompt can drive the same selector UI as a
    first-class ``ApprovalRequested`` event.
    """

    def __init__(self, *, action: str, risk: str, args_preview: str, approval_id: str = "") -> None:
        self.action = action
        self.risk = risk
        self.args_preview = args_preview
        self.approval_id = approval_id


def _parse_approval_prompt(text: str) -> Optional[_ApprovalRequest]:
    """Turn an engine approval prompt into an ``_ApprovalRequest``, or ``None``.

    Extracts the tool name for the headline action and keeps the full prompt
    body (minus the trailing "Reply 'yes'…" instruction line, which the selector
    replaces with its own options) as the preview.
    """
    stripped = text.lstrip()
    if not stripped.startswith(_APPROVAL_MARKERS):
        return None
    escalation = stripped.startswith("[SANDBOX ESCALATION]")
    m = re.search(r"tool '([^']+)'", stripped)
    tool = m.group(1) if m else "action"
    action = f"escalate: {tool}" if escalation else f"run: {tool}"
    # Drop the final "Reply 'yes' … 'no'." instruction line; the menu supplies it.
    lines = [ln for ln in text.splitlines() if not ln.strip().lower().startswith("reply ")]
    preview = "\n".join(lines).strip()
    return _ApprovalRequest(
        action=action,
        risk="high" if escalation else "medium",
        args_preview=preview,
    )


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

    def role_names(self) -> list:
        return []

    async def ask_user(self, question: str, sent_from: Any = None) -> str:
        # Approval prompts (rendered by the PermissionEngine and routed through
        # ``request_approval`` → here) drive the port's structured selector rather
        # than a free-text input, then map the choice back to the engine's reply
        # vocabulary. Non-approval questions fall through to the plain ``ask``.
        approval = _parse_approval_prompt(question)
        if approval is not None and hasattr(self._port, "decide_approval"):
            decision = await self._port.decide_approval(self._ctx, approval)
            outcome = getattr(decision, "outcome", "reject")
            return _OUTCOME_TO_REPLY.get(outcome, "no")

        return await self._port.ask(self._ctx, question)

    async def ask_user_question(self, questions: Any, sent_from: Any = None) -> Any:
        """Route structured multiple-choice questions to the port's ``ask_questions``.

        The structured counterpart of ``ask_user`` behind the ``AskUserQuestion``
        tool: a full round-trip (down as display, back up as structured answers)
        with zero text parsing. Ports that predate ``ask_questions`` degrade
        per-question through the plain ``ask``, still building structured answers.
        """
        if hasattr(self._port, "ask_questions"):
            return await self._port.ask_questions(self._ctx, questions)
        return await self._degrade_ask_questions(questions)

    async def _degrade_ask_questions(self, questions: Any) -> Any:
        """Loss-free per-question fallback for ports without ``ask_questions``.

        Builds *structured* answers, so — unlike the old text round-trip — there
        is no ``\\n\\n`` block splitting and no line-position pairing: bug #1 / #2
        cannot recur even on this path.
        """
        from mote.common.schema import AskUserQuestionAnswer, AskUserQuestionAnswers

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

    async def request_approval(self, request: Any, sent_from: Any = None) -> Any:
        """Route a gated action to the port for a structured approval decision.

        The inbound half of the ``ApprovalRequested`` contract. This exists so a
        Role capability can gate an action uniformly across platforms; wiring the
        *framework* side (a permission-engine callback → this method) is pending
        until the agent spine surfaces an approval AgentEvent — the contract is
        laid down now so the round-trip is not a later retrofit.
        """
        return await self._port.decide_approval(self._ctx, request)

    async def reply_to_user(self, content: str, sent_from: Any = None) -> str:
        return ""

    def set_addresses(self, role: Any, addresses: Any) -> None:  # noqa: D401 — no-op
        pass

    def publish_message(self, msg: Any) -> None:  # noqa: D401 — no-op
        pass


__all__ = ["PortHumanChannel"]
