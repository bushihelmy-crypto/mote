"""HookSubscriber — adapts the existing HookManager onto the event bus.

The hook layer is preserved intact; this thin subscriber is the single seam that
feeds it. It is the **adapter** between two vocabularies:

* the CC/codex-shaped :class:`HookOutcome` that ``HookManager.fire`` folds
  (behavior / updated_args / updated_response / additional_context / stop / ...),
  which stays the hook layer's own DTO, and
* the bus's **typed per-event outcomes** (``ToolCallOutcome`` / ``PromptOutcome``
  / ...), one per control event, that the emitter reads.

For each control event this subscriber ``handles``, it fires the matching hook
and then projects the folded :class:`HookOutcome` onto that event's typed
outcome, keeping only the fields meaningful for that event. Advisory events
(SessionStart / PostCompact / FileChanged) fire their hook for side effects and
contribute no outcome (``None``).

The whole per-event mapping lives in one table, :data:`_BINDINGS`, keyed by the
event's ``name`` discriminator — the same key the bus routes on and that
``handles`` is derived from. Each :class:`_HookBinding` co-locates the three
facts about an event that used to be split across two parallel ``isinstance``
chains: which hook to fire, how to build its payload, and how to project the
folded outcome back. Adding a control event is then a single table row, and the
routing/payload/projection for an event can never drift out of sync.

It runs at :attr:`ControlStage.REWRITE` so, in the one shared bucket
(``PreToolUse``), a hook that rewrites args lands *before* the permission gate
evaluates the rewritten call.

Not exported from ``common/hook/__init__`` on purpose: importing it pulls in
``common.events``, so it is imported lazily at the wiring site (Role) to keep the
hook package's import graph minimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from metagpt.common.events.outcomes import (
    CompactOutcome,
    PromptOutcome,
    ToolCallOutcome,
    ToolResultOutcome,
    TurnOutcome,
)
from metagpt.common.events.types import (
    FILE_CHANGED,
    POST_COMPACT,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_START,
    TURN_END,
    USER_PROMPT_SUBMIT,
)
from metagpt.common.hook.types import HookOutcome
from metagpt.common.interface.event_subscriber import ControlOutcome, ControlStage, ControlSubscriber


@dataclass(frozen=True)
class _HookBinding:
    """How one control event maps onto the hook layer, in one place.

    ``hook_name`` is the hook to fire; ``payload`` builds its input dict from the
    event; ``project`` translates the folded :class:`HookOutcome` into the event's
    typed :class:`ControlOutcome`. ``project=None`` marks an *advisory* event: the
    hook fires for its side effects but contributes no outcome.
    """

    hook_name: str
    payload: Callable[[object], dict]
    project: Optional[Callable[[HookOutcome], ControlOutcome]] = None


#: The per-event mapping, keyed by the event ``name`` discriminator the bus routes
#: on. Attribute access in the ``payload`` builders is duck-typed (the bus never
#: needs the event classes either), keeping the import surface minimal.
_BINDINGS: dict[str, _HookBinding] = {
    USER_PROMPT_SUBMIT: _HookBinding(
        "UserPromptSubmit",
        lambda e: {"prompt": e.prompt},
        lambda ho: PromptOutcome(
            additional_context=list(ho.additional_context),
            stop=ho.stop,
            stop_reason=ho.stop_reason,
        ),
    ),
    PRE_TOOL_USE: _HookBinding(
        "PreToolUse",
        lambda e: {
            "tool_name": e.tool_name,
            "tool_input": e.tool_input,
            "tool_use_id": e.tool_use_id,
        },
        lambda ho: ToolCallOutcome(
            behavior=ho.behavior,
            updated_args=ho.updated_args,
            system_message=ho.system_message,
            stop=ho.stop,
            stop_reason=ho.stop_reason,
        ),
    ),
    POST_TOOL_USE: _HookBinding(
        "PostToolUse",
        lambda e: {
            "tool_name": e.tool_name,
            "tool_input": e.tool_input,
            "tool_response": e.tool_response,
            "success": e.success,
            "error": (e.error.as_dict() if e.error is not None else None),
            "tool_use_id": e.tool_use_id,
        },
        lambda ho: ToolResultOutcome(
            updated_response=ho.updated_response,
            additional_context=list(ho.additional_context),
            blocked=ho.is_blocking,
            system_message=ho.system_message,
            stop_reason=ho.stop_reason,
        ),
    ),
    PRE_COMPACT: _HookBinding(
        "PreCompact",
        lambda e: {"trigger": e.trigger},
        lambda ho: CompactOutcome(cancel=ho.stop, additional_context=list(ho.additional_context)),
    ),
    TURN_END: _HookBinding(
        # The legacy "Stop" hook fired at the turn boundary. A "Stop" hook that
        # folds a deny is asking to *block the stop* — i.e. auto-continue.
        "Stop",
        lambda e: {},
        lambda ho: TurnOutcome(
            block=ho.behavior == "deny",
            additional_context=list(ho.additional_context),
            system_message=ho.system_message,
        ),
    ),
    # Advisory events: fire the hook for its side effects, contribute no outcome.
    POST_COMPACT: _HookBinding(
        "PostCompact",
        lambda e: {"trigger": e.trigger, "compact_summary": e.summary},
    ),
    SESSION_START: _HookBinding("SessionStart", lambda e: {"source": e.source}),
    FILE_CHANGED: _HookBinding(
        "FileChanged",
        lambda e: {
            "path": e.path,
            "change_type": e.change_type,
            "mtime": e.mtime,
            "size": e.size,
        },
    ),
}


class HookSubscriber(ControlSubscriber):
    """Routes control events to the wrapped :class:`HookManager`.

    Exposing ``handle_control`` (not ``handle``) is what places this subscriber on
    the bus's **control plane**: the bus routes the events it ``handles`` to it
    inline and folds the typed :class:`ControlOutcome` it returns into the value
    the emitter reads.
    """

    #: The control events this subscriber fires a hook for (bus routing keys) —
    #: exactly the table's keys, so the two can never disagree.
    handles: tuple[str, ...] = tuple(_BINDINGS)
    #: Rewrite stage — a hook that mutates args runs before the permission gate.
    stage: ControlStage = ControlStage.REWRITE
    #: Provenance label the bus stamps onto any arg/response rewrite a hook folds.
    name: str = "hook"

    def __init__(self, hook_manager) -> None:
        self._hook = hook_manager

    async def handle_control(self, event) -> Optional[ControlOutcome]:
        binding = _BINDINGS.get(event.name)
        if binding is None:
            return None
        outcome = await self._hook.fire(binding.hook_name, binding.payload(event))
        return binding.project(outcome) if binding.project is not None else None


__all__ = ["HookSubscriber"]
