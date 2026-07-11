"""PermissionSubscriber — the permission gate as a control-plane subscriber.

The permission engine used to be a *direct call* wedged into ``run_command``
right after the ``PreToolUse`` emit. That made it a hidden second vetoer the bus
knew nothing about. This subscriber puts the gate *on* the control plane so it is
a first-class, ordered, foldable influence — exactly like the hook layer — while
the engine itself stays tool-free (it only ever sees the tiny
:class:`~mote.common.schema.PermissionFacts` bundle the executor resolves).

Three properties place it precisely on the plane:

* ``handles = (PRE_TOOL_USE,)`` routes only tool-call events to it — it is never
  even invoked for anything else.
* ``stage = ControlStage.GATE`` runs it **after** the hook subscriber
  (``ControlStage.REWRITE``) in the shared ``PreToolUse`` bucket. Control
  subscribers run as a chained reduce, so by the time the gate evaluates the call
  it observes the arguments *already rewritten by any hook* — the ordering bug a
  naive peer wiring would introduce is structurally impossible.
* ``fail_mode = FAIL_CLOSED`` makes a crash/timeout in the gate **deny** the
  call rather than fall through — a security gate that "could not decide" must
  fail safe, unlike the advisory hook which fails open. The paired
  ``on_failure`` supplies the typed deny the bus folds in that case.

It reads the tool facts through ``event.resolve_facts`` (an executor-supplied,
tool-bound closure), so this module — and the bus beneath it — never imports a
tool. The engine already resolves any ``ask`` internally, so the decision handed
back is always terminal allow/deny; it is translated into the typed
:class:`ToolCallOutcome` the bus folds.
"""
from __future__ import annotations

from typing import Optional

from mote.common.events.outcomes import ToolCallOutcome
from mote.common.events.types import PRE_TOOL_USE, PreToolUseEvent
from mote.common.interface.event_subscriber import FAIL_CLOSED, ControlStage, ControlSubscriber
from mote.common.schema.permission_types import PermissionDecision
from mote.executor.permission.engine import PermissionEngine


class PermissionSubscriber(ControlSubscriber):
    """Routes :class:`PreToolUseEvent`\\s through the :class:`PermissionEngine`."""

    #: Only tool-call events reach this subscriber (bus routing key).
    handles: tuple[str, ...] = (PRE_TOOL_USE,)
    #: Gate stage — run after the hook rewriter so it sees rewritten args.
    stage: ControlStage = ControlStage.GATE
    #: A gate that cannot evaluate must deny, not wave the call through.
    fail_mode: str = FAIL_CLOSED
    #: Provenance label the bus stamps onto any argument narrowing this gate folds.
    name: str = "permission"

    def __init__(self, engine: PermissionEngine) -> None:
        self._engine = engine

    async def handle_control(self, event) -> Optional[ToolCallOutcome]:
        # Only tool calls are gated; everything else is not ours to judge.
        if not isinstance(event, PreToolUseEvent):
            return None
        resolve = getattr(event, "resolve_facts", None)
        if resolve is None:
            # No executor-supplied resolver (e.g. a bare event) — nothing to check.
            return None

        facts = resolve(event.tool_input)

        # Most tools touch a single target; a few (ApplyPatch) act on several
        # paths in one call. Evaluate them together via check_multi so a
        # multi-path call yields one consolidated approval; single-target tools
        # keep the segment-aware check() path.
        if len(facts.targets) > 1:
            decision = await self._engine.check_multi(
                event.tool_name,
                targets=facts.targets,
                tool_check=facts.tool_check,
                mutates_fs=facts.mutates_fs,
            )
        else:
            decision = await self._engine.check(
                event.tool_name,
                target=facts.targets[0] if facts.targets else "",
                tool_check=facts.tool_check,
                mutates_fs=facts.mutates_fs,
                segments=facts.segments,
            )
        return self._to_outcome(decision)

    @staticmethod
    def _to_outcome(decision: PermissionDecision) -> ToolCallOutcome:
        """Translate a terminal engine decision into a foldable outcome.

        The engine never returns ``ask`` (it prompts internally), so only
        ``deny``/``allow`` reach here. A deny carries its message so the executor
        surfaces the reason; an allow carries any argument narrowing the engine
        applied (``updated_args``) so it threads on through the fold.

        Two flavours of deny are distinguished by the decision's ``reason.type``:
        a genuine **user** rejection at the approval prompt (``"user"``) sets
        ``stop`` — the human said "no", so the react loop should end rather than
        let the model replan around it. Every other deny (rule / mode / policy /
        sandbox / fail-closed) is a recoverable block: it fails this one call but
        the loop keeps going so the model can choose a different action.
        """
        if decision.behavior == "deny":
            stop = decision.reason.type == "user"
            return ToolCallOutcome(
                behavior="deny",
                system_message=decision.message,
                stop=stop,
                stop_reason=decision.message if stop else "",
            )
        return ToolCallOutcome(behavior="allow", updated_args=decision.updated_args)

    @staticmethod
    def on_failure(reason: str) -> ToolCallOutcome:
        """Typed deny the bus folds when this gate itself crashes/times out."""
        return ToolCallOutcome(behavior="deny", system_message=reason)


__all__ = ["PermissionSubscriber"]
