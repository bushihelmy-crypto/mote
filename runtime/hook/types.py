"""Hook types — pure data, dependency-free (like ``permission/types.py``).

Kept free of any executor/roles/context import so it sits at the very bottom of
the layering and can be imported from anywhere without a cycle. ``HookBehavior``
aliases the canonical ``PermissionBehavior`` from ``common/schema`` (also a
pure-data, executor-free module) so the allow/deny/ask Literal has a single
source of truth. The executor seam (``ToolExecutor.run_command``) is the single
place that folds a neutral :class:`HookOutcome` back into a real
``PermissionDecision``.

The wire contract is JSON-on-stdin/JSON-on-stdout with decision fields
(``decision``/``permissionDecision``/``continue``/``additionalContext``/
``updatedInput``/``systemMessage``) and aggregation precedence deny > ask > allow.
The ``Stop`` event is the sole termination signal.
"""

from __future__ import annotations

from typing import Iterable, Optional

from mote.contracts.hook import HookBehavior, HookEvent, HookOutcome

# A shared, read-only-by-convention empty outcome (the no-op fast path).
EMPTY = HookOutcome()


def _behavior_rank(behavior: Optional[HookBehavior]) -> int:
    """Precedence rank: deny > ask > allow > (none). Higher wins."""
    return {"deny": 3, "ask": 2, "allow": 1}.get(behavior or "", 0)


def fold(outcomes: Iterable[HookOutcome]) -> HookOutcome:
    """Aggregate per-handler outcomes into one (deny-wins fold).

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
    "HookOutcome",
    "EMPTY",
    "fold",
]
