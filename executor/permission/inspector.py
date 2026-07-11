"""ToolCallInspector — the clean seam for hanging a PreToolUse gate.

A capability ceiling, an allowlist, a per-agent quota — all of these are the
same shape: *look at the tool call about to run and decide allow / deny*. The
control plane already carries that decision (a :class:`PreToolUseEvent` fanned
out to control subscribers that may fold a :class:`ToolCallOutcome`), but writing
one correctly means re-deriving the whole :class:`ControlSubscriber` contract:
the ``handles`` routing key, the ``GATE`` stage so it runs *after* the hook
rewriter, ``fail_mode = FAIL_CLOSED`` (a security gate that cannot decide must
deny), the paired ``on_failure`` typed deny, and the allow/deny →
:class:`ToolCallOutcome` translation. That is a lot of plane mechanics for what
should be one line of policy.

This base captures all of it once. A gate author subclasses it and implements a
single :meth:`inspect` returning a tiny :class:`Inspection` verdict — never
touching the EventBus, the outcome types, or the staging rules. Subscribe the
instance on the same bus the executor uses and it lands in the ``PreToolUse``
bucket automatically, ordered after any hook rewrite so it judges the *final*
args.

Like the :class:`~mote.executor.permission.subscriber.PermissionSubscriber`,
an inspector reads tool facts through the event's ``resolve_facts`` closure (the
executor attaches it — it owns the tool), so this module never imports a tool.
The facts are optional: a name/allowlist gate ignores them; a path/quota gate
reads them. It sits alongside the permission gate in the same ``GATE`` stage; the
bucket folds every gate's verdict deny-wins, so inspectors compose with the
permission engine and with each other without any of them knowing the others
exist.

Leaf placement: imports only the control-plane protocol constants
(``common/interface``) and the event/outcome leaf modules (``common/events``) —
never the bus, the hook, or the executor — so it introduces no cycle.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Optional

from mote.common.events.outcomes import ToolCallOutcome
from mote.common.events.types import PRE_TOOL_USE, PreToolUseEvent
from mote.common.interface.event_subscriber import FAIL_CLOSED, ControlStage, ControlSubscriber


@dataclass(frozen=True)
class Inspection:
    """A gate's verdict on one tool call — allow, or deny with a reason.

    Deliberately tiny: an inspector answers *may this call run?* and nothing
    else (rewriting args is the hook's job, at the REWRITE stage). A deny is a
    *recoverable* block — it fails this one call and surfaces ``reason`` to the
    model, which keeps going and can choose a different action; it never sets the
    loop-ending ``stop`` (that is reserved for a human saying "no" at the approval
    prompt, which only the permission engine issues).
    """

    allow: bool = True
    reason: str = ""

    @classmethod
    def allowed(cls) -> "Inspection":
        """The call passes this gate — defer to the rest of the bucket."""
        return cls(allow=True)

    @classmethod
    def denied(cls, reason: str) -> "Inspection":
        """Block the call; ``reason`` is surfaced to the model."""
        return cls(allow=False, reason=reason)


class ToolCallInspector(ControlSubscriber):
    """Base for a PreToolUse allow/deny gate — subclass, implement :meth:`inspect`.

    All the control-plane wiring lives here; a subclass supplies only the policy.
    Override the class attribute :attr:`name` for provenance/logging; override
    :attr:`fail_mode` to ``FAIL_OPEN`` only for a purely advisory (non-security)
    inspector — the default fails closed, denying on its own crash/timeout.
    """

    #: Only tool-call events reach this subscriber (bus routing key).
    handles: tuple[str, ...] = (PRE_TOOL_USE,)
    #: Gate stage — judge the call *after* the hook rewriter, alongside the
    #: permission gate (the shared ``PreToolUse`` GATE bucket folds deny-wins).
    stage: ControlStage = ControlStage.GATE
    #: A security gate that cannot evaluate must deny, not wave the call through.
    fail_mode: str = FAIL_CLOSED
    #: Provenance label the bus stamps onto any influence this gate folds.
    name: str = "inspector"

    @abstractmethod
    async def inspect(self, tool_name: str, tool_input: dict, facts) -> Inspection:
        """Return the verdict for one tool call.

        Args:
            tool_name: The tool about to run.
            tool_input: Its arguments — already rewritten by any earlier hook.
            facts: The tool-bound :class:`~mote.common.schema.PermissionFacts`
                (targets / mutates_fs / ...) resolved from the current args, or
                ``None`` when no resolver is wired. A name/allowlist gate ignores
                it; a path/quota gate reads it.
        """
        ...

    async def handle_control(self, event) -> Optional[ToolCallOutcome]:
        """Route a :class:`PreToolUseEvent` through :meth:`inspect` → outcome.

        Non-tool events are not ours to judge (``None``). The tool facts are
        resolved from the *current* (possibly hook-rewritten) args via the
        executor-supplied closure, then handed to :meth:`inspect`; an ``allow``
        folds an inert allow (defer to the bucket) and a ``deny`` folds a
        recoverable block carrying the reason.
        """
        if not isinstance(event, PreToolUseEvent):
            return None
        resolve = getattr(event, "resolve_facts", None)
        facts = resolve(event.tool_input) if resolve is not None else None
        verdict = await self.inspect(event.tool_name, event.tool_input, facts)
        if verdict.allow:
            return ToolCallOutcome(behavior="allow")
        return ToolCallOutcome(behavior="deny", system_message=verdict.reason)

    def on_failure(self, reason: str) -> ToolCallOutcome:
        """Typed deny the bus folds when this gate itself crashes/times out.

        Required of any ``FAIL_CLOSED`` subscriber (the bus is generic and cannot
        know which outcome type to synthesize).
        """
        return ToolCallOutcome(behavior="deny", system_message=reason)


__all__ = ["Inspection", "ToolCallInspector"]
