"""Hook types — pure data, dependency-free (like ``permission/types.py``).

Kept free of any executor/roles/context import so it sits at the very bottom of
the layering and can be imported from anywhere without a cycle. ``HookBehavior``
aliases the canonical ``PermissionBehavior`` from ``common/schema`` (also a
pure-data, executor-free module) so the allow/deny/ask Literal has a single
source of truth. The executor seam (``ToolExecutor.run_command``) is the single
place that folds a neutral :class:`HookOutcome` back into a real
``PermissionDecision``.

Two synthesized references:
  * **Claude Code** — JSON-on-stdin/JSON-on-stdout contract, decision fields
    (``decision``/``permissionDecision``/``continue``/``additionalContext``/
    ``updatedInput``/``systemMessage``), aggregation precedence deny > ask > allow.
  * **Codex** — the same Claude-style engine plus a legacy fire-and-forget notify
    (subsumed here by the ``Stop`` event).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

from mote.common.schema.permission_types import PermissionBehavior

# ---------------------------------------------------------------------------
# Enumerations (Literals — no runtime enum machinery)
# ---------------------------------------------------------------------------

# The lifecycle events a hook may fire on (phase 1 set). CC/codex compatible
# names so external command handlers stay drop-in.
HookEvent = Literal[
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "SessionStart",
    "Stop",
    "PreCompact",
    "PostCompact",
    "FileChanged",
]

# Alias the canonical allow/deny/ask Literal (single source of truth in
# ``common/schema/permission_types``) — same values, no duplicated declaration.
HookBehavior = PermissionBehavior


# ---------------------------------------------------------------------------
# HookInput — what a handler receives
# ---------------------------------------------------------------------------


@dataclass
class HookInput:
    """The payload handed to a hook handler.

    The common envelope mirrors Claude Code's hook input: identity + the event
    name + the active permission mode, plus a free-form ``payload`` carrying the
    per-event fields (tool_name/tool_input/tool_response, prompt, trigger, ...).

    ``to_json_dict`` renders the CC/codex wire shape (camelCase top-level keys
    merged with the payload) used as the JSON stdin for command handlers.
    """

    hook_event_name: str
    session_id: str = ""
    cwd: str = ""
    transcript_path: str = ""
    permission_mode: Optional[str] = None
    payload: dict = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        """Render the JSON object delivered on a command handler's stdin.

        Top-level identity keys use the CC camelCase names; the per-event
        ``payload`` fields are merged in at the top level too (so a handler reads
        ``toolName``/``toolInput`` directly, matching the reference contract).
        """
        wire: dict = {
            "hook_event_name": self.hook_event_name,
            "hookEventName": self.hook_event_name,
            "session_id": self.session_id,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "transcript_path": self.transcript_path,
            "transcriptPath": self.transcript_path,
        }
        if self.permission_mode is not None:
            wire["permission_mode"] = self.permission_mode
            wire["permissionMode"] = self.permission_mode
        # Per-event fields. Merged last so an event payload can override the
        # envelope if it ever needs to (it normally carries disjoint keys).
        wire.update(self.payload)
        return wire


# ---------------------------------------------------------------------------
# HookOutcome — the aggregated result of firing an event
# ---------------------------------------------------------------------------


@dataclass
class HookOutcome:
    """The (folded) influence a hook event has on the host.

    Neutral by design — it does NOT reference ``PermissionDecision`` so it can
    live at the bottom layer. The executor seam translates the ``behavior`` /
    ``updated_args`` into a real ``PermissionDecision`` for tool-call influence;
    other consumers read ``additional_context`` / ``system_message`` / ``stop``.

    ``updated_response`` is the *output* analogue of ``updated_args``: a
    PostToolUse control subscriber may rewrite the tool's result text (e.g. to
    truncate/redact it) and the executor applies the rewrite to the result
    before the model sees it. It threads forward on the plane just like
    ``updated_args`` so a later subscriber observes the already-rewritten output.
    """

    behavior: Optional[HookBehavior] = None
    updated_args: Optional[dict] = None
    updated_response: Optional[str] = None
    additional_context: list[str] = field(default_factory=list)
    system_message: str = ""
    stop: bool = False
    stop_reason: str = ""

    @property
    def is_blocking(self) -> bool:
        """True when the outcome blocks the action (deny) or halts the agent."""
        return self.behavior == "deny" or self.stop


# A shared, read-only-by-convention empty outcome (the no-op fast path).
EMPTY = HookOutcome()


def _behavior_rank(behavior: Optional[HookBehavior]) -> int:
    """Precedence rank: deny > ask > allow > (none). Higher wins."""
    return {"deny": 3, "ask": 2, "allow": 1}.get(behavior or "", 0)


def fold(outcomes: Iterable[HookOutcome]) -> HookOutcome:
    """Aggregate per-handler outcomes into one (CC/codex deny-wins fold).

    Precedence for ``behavior``: **deny > ask > allow**. A ``deny`` (or ``ask``)
    is immune to a later ``allow`` — once the result reaches a higher rank, a
    lower-ranked behavior never overrides it. ``additional_context`` accumulates
    across all handlers (order preserved); ``system_message`` takes the last
    non-empty value; ``stop`` is sticky (any handler stopping stops the fold);
    ``updated_args`` / ``updated_response`` each take the last handler that
    supplied one.
    """
    result = HookOutcome()
    best_rank = 0
    for outcome in outcomes:
        rank = _behavior_rank(outcome.behavior)
        if rank > best_rank:
            best_rank = rank
            result.behavior = outcome.behavior
        if outcome.updated_args is not None:
            result.updated_args = outcome.updated_args
        if outcome.updated_response is not None:
            result.updated_response = outcome.updated_response
        if outcome.additional_context:
            result.additional_context.extend(outcome.additional_context)
        if outcome.system_message:
            result.system_message = outcome.system_message
        if outcome.stop:
            result.stop = True
            if outcome.stop_reason:
                result.stop_reason = outcome.stop_reason
    return result


__all__ = [
    "HookEvent",
    "HookBehavior",
    "HookInput",
    "HookOutcome",
    "EMPTY",
    "fold",
]
