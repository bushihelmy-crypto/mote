"""Approval prompt text + response parsing.

When a decision resolves to ``ask``, the engine renders a question with
:func:`build_approval_prompt` and sends it through the Role's
``request_approval`` capability (which reaches the human via the env). The
user's free-text reply is interpreted by :func:`parse_approval_response`.
"""
from __future__ import annotations

from typing import Literal

# What the user's reply maps to.
#   allow_once    -> run this call only
#   allow_session -> run and remember for the session
#   deny          -> block this call
ApprovalChoice = Literal["allow_once", "allow_session", "deny"]


def build_approval_prompt(tool_name: str, target: str, reason: str = "") -> str:
    """Compose the approval question shown to the user."""
    target_line = f"\n  target: {target}" if target else ""
    reason_line = f"\n  reason: {reason}" if reason else ""
    return (
        f"[APPROVAL REQUIRED] The agent wants to run tool '{tool_name}'."
        f"{target_line}{reason_line}\n"
        "Reply 'yes' to allow once, 'always' to allow for the rest of the session, "
        "or 'no' to deny."
    )


def build_escalation_prompt(tool_name: str, path: str, reason: str) -> str:
    """Compose a sandbox-violation escalation question.

    Distinct from a plain approval prompt: the action is permitted by policy but
    would cross the sandbox boundary, so the user is asked to grant an exception.
    """
    return (
        f"[SANDBOX ESCALATION] Tool '{tool_name}' wants to write outside the sandbox:\n"
        f"  path:   {path}\n"
        f"  reason: {reason}\n"
        "Reply 'yes' to allow this write once, 'always' to allow writes under this "
        "directory for the session, or 'no' to block it."
    )


def parse_approval_response(response: str) -> ApprovalChoice:
    """Interpret a free-text approval reply. Unknown replies fail closed (deny)."""
    text = (response or "").strip().lower()
    if not text:
        return "deny"
    # "always"/"session"/"all" -> persist for the session (check before plain allow).
    if any(k in text for k in ("always", "session", "allow all", "all of them")):
        return "allow_session"
    if any(text.startswith(k) for k in ("y", "allow", "approve", "ok", "sure", "yes")):
        return "allow_once"
    if "yes" in text:
        return "allow_once"
    return "deny"
