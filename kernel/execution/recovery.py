"""Effect-aware lifecycle and recovery around the Kernel graph runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Generic, TypeAlias, TypeVar

from mote.kernel.execution.graph import AgentGraph, AgentNode, EffectKind, End, GraphRunner, NodeId, Transition

StateT = TypeVar("StateT")
ResultT = TypeVar("ResultT")
NodeLifecycleCallback: TypeAlias = Callable[["NodeAttempt"], Awaitable[None] | None]


class RecoveryDirective(str, Enum):
    """The only legal crash-resume treatment for a node effect class."""

    RESTART = "restart"
    REINSTATE = "reinstate"
    RECONCILE = "reconcile"
    RESUME_WAIT = "resume_wait"


_RECOVERY_BY_EFFECT = {
    EffectKind.PURE: RecoveryDirective.RESTART,
    EffectKind.REPLAYABLE: RecoveryDirective.REINSTATE,
    EffectKind.LEDGERED: RecoveryDirective.RECONCILE,
    EffectKind.EXTERNAL: RecoveryDirective.RECONCILE,
    EffectKind.WAITABLE: RecoveryDirective.RESUME_WAIT,
}


@dataclass(frozen=True)
class NodeAttempt:
    """Immutable lifecycle identity for one node invocation."""

    node_id: NodeId
    effect_kind: EffectKind
    recovery: RecoveryDirective


class EffectAwareGraphRunner(Generic[StateT, ResultT]):
    """Run one graph and terminally resolve in-process failure paths.

    A process crash is the only path that skips these callbacks, deliberately
    leaving journal state for resume-time reconciliation. Cancellation is an
    abandonment, while an ordinary exception is a failed attempt.
    """

    def __init__(
        self,
        graph: AgentGraph[StateT, ResultT],
        *,
        on_cancel: Callable[[], None],
        on_failure: Callable[[], None],
        max_steps: int = 100_000,
        on_node_started: NodeLifecycleCallback | None = None,
        on_node_completed: NodeLifecycleCallback | None = None,
        on_node_abandoned: NodeLifecycleCallback | None = None,
        on_node_failed: NodeLifecycleCallback | None = None,
    ) -> None:
        self._runner = GraphRunner(graph, max_steps=max_steps, execute_node=self._execute_node)
        self._on_cancel = on_cancel
        self._on_failure = on_failure
        self._on_node_started = on_node_started
        self._on_node_completed = on_node_completed
        self._on_node_abandoned = on_node_abandoned
        self._on_node_failed = on_node_failed
        self._active_attempt: NodeAttempt | None = None

    async def _execute_node(
        self,
        node: AgentNode[StateT, ResultT],
        state: StateT,
    ) -> Transition | End[ResultT]:
        attempt = NodeAttempt(
            node_id=node.node_id,
            effect_kind=node.effect_kind,
            recovery=_RECOVERY_BY_EFFECT[node.effect_kind],
        )
        self._active_attempt = attempt
        if self._on_node_started is not None:
            await self._invoke(self._on_node_started, attempt)
        try:
            outcome = await node.run(state)
        except asyncio.CancelledError:
            if self._on_node_abandoned is not None:
                await self._invoke(self._on_node_abandoned, attempt)
            raise
        except Exception:
            if self._on_node_failed is not None:
                await self._invoke(self._on_node_failed, attempt)
            raise
        else:
            if self._on_node_completed is not None:
                await self._invoke(self._on_node_completed, attempt)
            return outcome
        finally:
            self._active_attempt = None

    @staticmethod
    async def _invoke(callback: NodeLifecycleCallback, attempt: NodeAttempt) -> None:
        outcome = callback(attempt)
        if outcome is not None:
            await outcome

    async def run(self, state: StateT) -> ResultT:
        try:
            return await self._runner.run(state)
        except asyncio.CancelledError:
            self._on_cancel()
            raise
        except Exception:
            self._on_failure()
            raise


__all__ = ["EffectAwareGraphRunner", "NodeAttempt", "RecoveryDirective"]
