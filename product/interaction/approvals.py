"""Approval console rendering + response parsing (non-port fallback).

The interactive approval round-trip is *structured*: the engine emits an
:class:`~mote.contracts.interaction.ApprovalRequest` and gets back an
:class:`~mote.contracts.interaction.ApprovalChoice`. The production
front-end (``PortHumanChannel``) drives a port's structured ``decide_approval``
selector, so it never needs prose.

This module serves only the headless text-input adapter, which has no structured selector. There
we must render the request to a line the human can read and parse their typed
reply back into a choice. The wording here is intentionally English: this path
is the developer/plumbing console (tests, headless), not the localized CLI —
the human-facing localized surface is the port selector in ``cli`` under i18n.
"""

from __future__ import annotations

from mote.contracts.interaction import ApprovalChoice, ApprovalRequest


def render_approval_prompt(request: ApprovalRequest) -> str:
    """Render an :class:`ApprovalRequest` to a single console prompt string.

    Used only by the ``get_human_input`` fallback (no structured selector). The
    ``target``/``paths`` are the verbatim code artifact; ``reason_detail`` (a
    tool self-check note or sandbox verdict) is shown as-is when present.
    """
    escalation = request.kind == "escalation"
    head = "[SANDBOX ESCALATION]" if escalation else "[APPROVAL REQUIRED]"
    verb = "write outside the sandbox" if escalation else f"run tool '{request.tool_name}'"
    lines = [f"{head} The agent wants to {verb}."]
    if request.target:
        label = "path" if escalation else "target"
        lines.append(f"  {label}: {request.target}")
    if request.reason_detail:
        lines.append(f"  reason: {request.reason_detail}")
    if request.suggestion:
        always = f"'always' to add the rule {request.suggestion} for the session"
    else:
        always = "'always' to allow for the rest of the session"
    lines.append(f"Reply 'yes' to allow once, {always}, or 'no' to deny.")
    return "\n".join(lines)


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
