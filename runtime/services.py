"""Engine-owned shared Runtime services and their lifecycle boundary."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable

from mote.contracts.ports.agent.budget import AgentBudgetPort
from mote.contracts.ports.session.run_lease import RunLeaseCoordinator
from mote.contracts.ports.workflow.delivery import WorkflowAgentDeliveryCompositionPort
from mote.contracts.ports.workflow.governance import WorkflowGovernanceCompositionPort
from mote.contracts.runtime.application import ApplicationCompositionPort, ApplicationReloadPort
from mote.runtime.code_map.scan_gate import CodeMapScanGate
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleResource, LifecycleStack
from mote.runtime.models.clients.context import Context

ENGINE_CONTEXT_CLOSE_PHASE = LifecyclePhase.RELEASE_CONTAINER


class EngineServices:
    """Shared infrastructure borrowed by Engine-managed Agent incarnations.

    An Engine closes this container after all of its Agents. Isolated spawn
    trees instead acquire one ownership lease per live incarnation; the final
    lease closes the container. This makes fork/spawn ownership compositional
    instead of copying a boolean that can double-close a shared Context.
    """

    __slots__ = (
        "context",
        "code_map_scan_gate",
        "run_lease_coordinator",
        "application_composition",
        "application_reloader",
        "agent_budget",
        "workflow_governance",
        "workflow_delivery",
        "_owner_count",
        "_owner_lock",
        "_owned_close_started",
        "_lifecycle",
    )

    def __init__(
        self,
        *,
        context: Context,
        run_lease_coordinator: RunLeaseCoordinator | None = None,
        code_map_scan_gate: CodeMapScanGate | None = None,
        resources: Iterable[LifecycleResource] = (),
        application_composition: ApplicationCompositionPort | None = None,
        application_reloader: ApplicationReloadPort | None = None,
        agent_budget: AgentBudgetPort | None = None,
        workflow_governance: WorkflowGovernanceCompositionPort | None = None,
        workflow_delivery: WorkflowAgentDeliveryCompositionPort | None = None,
    ) -> None:
        self.context = context
        self.application_composition = application_composition
        self.application_reloader = application_reloader
        self.agent_budget = agent_budget
        self.workflow_governance = workflow_governance
        self.workflow_delivery = workflow_delivery
        self.run_lease_coordinator = run_lease_coordinator
        self.code_map_scan_gate = code_map_scan_gate or CodeMapScanGate()
        self._owner_count = 0
        self._owner_lock = threading.Lock()
        self._owned_close_started = False
        self._lifecycle = LifecycleStack()
        for resource in resources:
            self._lifecycle.register(resource)
        self._lifecycle.register_close(
            "runtime-context",
            self.context.aclose,
            phase=ENGINE_CONTEXT_CLOSE_PHASE,
        )

    def register_resource(self, resource: LifecycleResource) -> None:
        """Add a shared Engine resource before ownership shutdown starts."""

        self._lifecycle.register(resource)

    def acquire(self) -> "EngineServicesLease":
        """Acquire lifecycle ownership for one isolated Agent incarnation."""

        with self._owner_lock:
            if self._owned_close_started:
                raise RuntimeError("EngineServices is closing and cannot accept a new owner.")
            self._owner_count += 1
        return EngineServicesLease(self)

    async def _release(self) -> None:
        close_services = False
        with self._owner_lock:
            if self._owner_count < 1:
                raise RuntimeError("EngineServices ownership underflow")
            if self._owner_count > 1:
                self._owner_count -= 1
                return
            self._owned_close_started = True
            close_services = True
        if close_services:
            try:
                await self._lifecycle.aclose()
            except BaseException:
                with self._owner_lock:
                    self._owned_close_started = False
                raise
            with self._owner_lock:
                self._owner_count = 0

    async def aclose(self) -> None:
        """Close Engine-owned services after every borrowed Agent has stopped."""

        with self._owner_lock:
            if self._owner_count:
                raise RuntimeError(
                    f"EngineServices still has {self._owner_count} isolated owner(s); "
                    "release their leases before Engine shutdown."
                )
        await self._lifecycle.aclose()


class EngineServicesLease:
    """Idempotent ownership claim for one isolated Agent incarnation."""

    __slots__ = ("services", "_close_task", "_released")

    def __init__(self, services: EngineServices) -> None:
        self.services = services
        self._close_task: asyncio.Task[None] | None = None
        self._released = False

    async def aclose(self) -> None:
        if self._released:
            return
        task = self._close_task
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(self._release(), name="mote-engine-services-release")
            self._close_task = task
        await asyncio.shield(task)

    async def _release(self) -> None:
        await self.services._release()
        self._released = True


__all__ = ["EngineServices", "EngineServicesLease"]
