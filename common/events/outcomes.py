"""Typed control-plane outcomes — one small DTO per control event.

Each control event answers a *different* question, so each gets its own outcome
type instead of one god-struct whose fields are meaningful only per-event. A
``PreToolUse`` subscriber decides *run this call?* (:class:`ToolCallOutcome`); a
``PreAgentSpawn`` subscriber decides *allow this child?* (:class:`SpawnOutcome`).
The type makes the contract explicit and unforgeable: a spawn gate structurally
*cannot* set ``updated_response`` because ``SpawnOutcome`` has no such field.

Every outcome satisfies the :class:`~metagpt.common.interface.event_subscriber.ControlOutcome`
protocol the bus drives generically:

* ``is_blocking`` — the outcome short-circuits the rest of the bucket (a deny/stop
  is final; no later subscriber can un-block it).
* ``merge(other)`` — fold two outcomes *of the same event* into one. Same
  precedence the hook layer uses (deny > ask > allow, accumulated context, last
  rewrite wins, sticky stop) but scoped to each event's own fields, so the fold
  is type-checked rather than convention.
* ``rebind(event, *, by)`` — thread this subscriber's rewrite forward so the
  next subscriber in the bucket observes the already-rewritten call, recording
  the change (with ``by`` = the rewriting subscriber's name, stamped by the bus)
  as provenance on the event. Only the two rewriting events (``PreToolUse`` args,
  ``PostToolUse`` output) do anything; the rest return the event unchanged — the
  identity default lives on :class:`_ControlOutcomeBase`, so a new non-rewriting
  outcome is inert for free.

Adding a new *control* event = add one outcome type here (with these three
methods) + a subscriber declaring ``handles``/``stage``. The bus loop and every
existing subscriber are untouched — buckets are keyed by event name, so there is
no global list to reorder and no shared struct to widen.

Leaf module: imports only ``dataclasses``/``typing`` + the pure-data
``PermissionBehavior`` Literal. It never imports the bus, hook, or any tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from metagpt.common.schema.permission_types import PermissionBehavior

# allow/deny/ask precedence — deny beats ask beats allow beats nothing.
_BEHAVIOR_RANK = {"deny": 3, "ask": 2, "allow": 1}


def _fold_behavior(a: Optional[PermissionBehavior], b: Optional[PermissionBehavior]) -> Optional[PermissionBehavior]:
    """Return the higher-precedence behavior (deny > ask > allow > None)."""
    return b if _BEHAVIOR_RANK.get(b or "", 0) > _BEHAVIOR_RANK.get(a or "", 0) else a


def _pick_last(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Take ``b`` when it is set (non-empty/non-None), else keep ``a`` (last-wins)."""
    return b if b else a


class _ControlOutcomeBase:
    """Shared default for the rebind axis: an outcome rewrites nothing.

    ``rebind`` is identity by default — the common case (a deny, a stop, a
    context injection mutates no event field). Only the two rewriting outcomes
    (:class:`ToolCallOutcome`, :class:`ToolResultOutcome`) override it, so
    "rewriting is the exception" is expressed structurally and a new outcome is
    inert — and correct — for free. ``is_blocking``/``merge`` stay per-event:
    they are genuinely different and intentionally not shared (no god-struct).
    """

    def rebind(self, event, *, by: str = ""):
        """Return ``event`` unchanged — this outcome rewrote no field."""
        return event


# ---------------------------------------------------------------------------
# PreToolUse — "run this tool call?"  (bucket: HookSubscriber, PermissionGate)
# ---------------------------------------------------------------------------


@dataclass
class ToolCallOutcome(_ControlOutcomeBase):
    """A tool call may be denied, or have its args rewritten, before it runs.

    The one two-subscriber bucket: the hook (rewrite/veto) then the permission
    gate (evaluate the rewritten args, allow/deny). ``merge`` folds them
    deny-wins; ``rebind`` threads a hook's ``updated_args`` to the gate.
    """

    behavior: Optional[PermissionBehavior] = None
    updated_args: Optional[dict] = None
    system_message: str = ""
    stop: bool = False
    stop_reason: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.behavior == "deny" or self.stop

    def merge(self, other: "ToolCallOutcome") -> "ToolCallOutcome":
        return ToolCallOutcome(
            behavior=_fold_behavior(self.behavior, other.behavior),
            updated_args=other.updated_args if other.updated_args is not None else self.updated_args,
            system_message=_pick_last(self.system_message, other.system_message) or "",
            stop=self.stop or other.stop,
            stop_reason=_pick_last(self.stop_reason, other.stop_reason) or "",
        )

    def rebind(self, event, *, by: str = ""):
        """Thread rewritten args forward, recording the rewrite on the event.

        Delegates to the event's generic :meth:`~metagpt.common.events.types.Rewritable.rewrite`
        so the before-image and ``by`` attribution are captured with the mutation.
        """
        if self.updated_args is not None and hasattr(event, "rewrite"):
            return event.rewrite("tool_input", self.updated_args, by=by)
        return event


# ---------------------------------------------------------------------------
# PostToolUse — "reshape this result?"  (bucket: HookSubscriber)
# ---------------------------------------------------------------------------


@dataclass
class ToolResultOutcome(_ControlOutcomeBase):
    """A finished tool's result may be rewritten, annotated, or marked blocked.

    ``updated_response`` replaces the output text (truncate/redact);
    ``additional_context`` is appended after it; ``blocked`` marks the result a
    failure the model must react to.
    """

    updated_response: Optional[str] = None
    additional_context: list[str] = field(default_factory=list)
    blocked: bool = False
    system_message: str = ""
    stop_reason: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.blocked

    def merge(self, other: "ToolResultOutcome") -> "ToolResultOutcome":
        return ToolResultOutcome(
            updated_response=(
                other.updated_response if other.updated_response is not None else self.updated_response
            ),
            additional_context=[*self.additional_context, *other.additional_context],
            blocked=self.blocked or other.blocked,
            system_message=_pick_last(self.system_message, other.system_message) or "",
            stop_reason=_pick_last(self.stop_reason, other.stop_reason) or "",
        )

    def rebind(self, event, *, by: str = ""):
        """Thread the rewritten output forward, recording the rewrite on the event.

        Delegates to the event's generic :meth:`~metagpt.common.events.types.Rewritable.rewrite`
        so the before-image and ``by`` attribution are captured with the mutation.
        """
        if self.updated_response is not None and hasattr(event, "rewrite"):
            return event.rewrite("tool_response", self.updated_response, by=by)
        return event


# ---------------------------------------------------------------------------
# UserPromptSubmit — "inject context / abort the turn?"  (bucket: HookSubscriber)
# ---------------------------------------------------------------------------


@dataclass
class PromptOutcome(_ControlOutcomeBase):
    """Context to prepend to the user prompt, or a stop that aborts the turn."""

    additional_context: list[str] = field(default_factory=list)
    stop: bool = False
    stop_reason: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.stop

    def merge(self, other: "PromptOutcome") -> "PromptOutcome":
        return PromptOutcome(
            additional_context=[*self.additional_context, *other.additional_context],
            stop=self.stop or other.stop,
            stop_reason=_pick_last(self.stop_reason, other.stop_reason) or "",
        )


# ---------------------------------------------------------------------------
# PreCompact — "veto the compaction / supply instructions?"  (bucket: HookSubscriber)
# ---------------------------------------------------------------------------


@dataclass
class CompactOutcome(_ControlOutcomeBase):
    """Cancel the management pass, or supply custom compaction instructions."""

    cancel: bool = False
    additional_context: list[str] = field(default_factory=list)

    @property
    def is_blocking(self) -> bool:
        return self.cancel

    def merge(self, other: "CompactOutcome") -> "CompactOutcome":
        return CompactOutcome(
            cancel=self.cancel or other.cancel,
            additional_context=[*self.additional_context, *other.additional_context],
        )


# ---------------------------------------------------------------------------
# PreAgentSpawn — "allow this child?"  (bucket: SpawnGate)
# ---------------------------------------------------------------------------


@dataclass
class SpawnOutcome(_ControlOutcomeBase):
    """Deny a child spawn (depth/quota/policy). ``reason`` surfaces to the caller."""

    denied: bool = False
    reason: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.denied

    def merge(self, other: "SpawnOutcome") -> "SpawnOutcome":
        return SpawnOutcome(
            denied=self.denied or other.denied,
            reason=_pick_last(self.reason, other.reason) or "",
        )


# ---------------------------------------------------------------------------
# TurnEnd — "block the stop to force another turn?"  (bucket: HookSubscriber)
# ---------------------------------------------------------------------------


@dataclass
class TurnOutcome(_ControlOutcomeBase):
    """A Stop-hook decision: ``block`` the turn end (auto-continue) + why."""

    block: bool = False
    additional_context: list[str] = field(default_factory=list)
    system_message: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.block

    def merge(self, other: "TurnOutcome") -> "TurnOutcome":
        return TurnOutcome(
            block=self.block or other.block,
            additional_context=[*self.additional_context, *other.additional_context],
            system_message=_pick_last(self.system_message, other.system_message) or "",
        )


__all__ = [
    "ToolCallOutcome",
    "ToolResultOutcome",
    "PromptOutcome",
    "CompactOutcome",
    "SpawnOutcome",
    "TurnOutcome",
]
