#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Localized rendering of a structured :class:`ApprovalRequest`.

The engine emits a language-neutral
:class:`~mote.contracts.interaction.ApprovalRequest`; both interactive
front-ends (the terminal arrow-key menu and the Textual modal) render it to
human-facing wording here, under the active i18n locale. Keeping this in one
place means the two ports show identical, localized text and the option
vocabulary stays in sync.

A tool self-check note (``reason_code == "tool"``) and a sandbox verdict
(``reason_code == "sandbox"``) carry author-written English in ``reason_detail``
and are shown verbatim; the fixed reasons (ask rule / mode default) are looked
up in the catalog.
"""
from __future__ import annotations

from typing import Tuple

from mote.contracts.interaction import ApprovalRequest
from mote.product.i18n import keys as K
from mote.product.i18n import t
from mote.product.presentation.events import ApprovalRequested

# The approval outcomes in display order, paired with their catalog key +
# single-key shortcut. Ports render the localized label; the outcome string is
# the ``ApprovalDecision.outcome`` the selector returns.
_APPROVAL_OPTION_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("accept", K.APPROVAL_OPT_YES, "y"),
    ("always_allow", K.APPROVAL_OPT_ALWAYS, "a"),
    ("reject", K.APPROVAL_OPT_NO, "n"),
    ("always_deny", K.APPROVAL_OPT_NEVER, "d"),
)


def approval_options() -> Tuple[Tuple[str, str, str], ...]:
    """The four ``(outcome, localized_label, shortcut)`` approval choices."""
    return tuple((outcome, t(key), shortcut) for outcome, key, shortcut in _APPROVAL_OPTION_SPECS)


def approval_action(request: ApprovalRequest | ApprovalRequested) -> str:
    """The localized headline: "run: <tool>" or "escalate: <tool>"."""
    tool = request.tool_name or (request.action if isinstance(request, ApprovalRequested) else "") or "action"
    key = (
        K.APPROVAL_ACTION_ESCALATE
        if isinstance(request, ApprovalRequest) and request.kind == "escalation"
        else K.APPROVAL_ACTION_RUN
    )
    return t(key, tool=tool)


def approval_risk(request: ApprovalRequest | ApprovalRequested) -> str:
    """The risk band (``low``/``medium``/``high``); a neutral, unlocalized label."""
    return request.risk or "medium"


def approval_reason(request: ApprovalRequest | ApprovalRequested) -> str:
    """The localized (fixed) or verbatim (tool/sandbox) reason line, or ""."""
    if isinstance(request, ApprovalRequested):
        return ""
    detail = request.reason_detail or ""
    if detail:
        return detail
    code = request.reason_code or ""
    if code == "ask_rule":
        return t(K.APPROVAL_REASON_ASK_RULE)
    if code == "default":
        return t(K.APPROVAL_REASON_DEFAULT)
    return ""


def approval_preview(request: ApprovalRequest | ApprovalRequested) -> str:
    """The multi-line body shown under the headline: target, reason, suggestion.

    Assembles the code artifact at stake (``target``), the reason line, and the
    session-rule hint an "always" grant would add — each on its own line, any
    empty part omitted. Ports that predate the structured request fall back to
    a plain ``args_preview`` string.
    """
    if isinstance(request, ApprovalRequested):
        return request.args_preview or ""
    target = request.target
    lines: list[str] = []
    if target:
        lines.append(target)
    reason = approval_reason(request)
    if reason:
        lines.append(reason)
    suggestion = request.suggestion or ""
    if suggestion:
        lines.append(t(K.APPROVAL_SUGGESTION, rule=suggestion))
    return "\n".join(lines)


__all__ = [
    "approval_options",
    "approval_action",
    "approval_risk",
    "approval_reason",
    "approval_preview",
]
