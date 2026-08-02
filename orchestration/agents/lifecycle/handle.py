#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ChildAgentHandle — the one shape every spawn site funnels through.

A handle is what :meth:`AgentControl.spawn_agent` hands back: a thin RAII wrapper
around the spawned :class:`AgentRuntime` + its committed
:class:`SpawnReservation`, so the four hand-written *construct → run → read →
cleanup* sites collapse into a single, consistent object.

Two drive shapes mirror :class:`~mote.contracts.agent.Lifecycle`:
  * ``EPHEMERAL`` — the caller runs the child inline with
    :meth:`run_to_completion` (one turn, summary read back), never entering the
    scheduler.
  * ``MANAGED`` — the child is already in the scheduler; the caller awaits its
    terminal status via :meth:`join`.

On every exit path (explicit :meth:`aclose`, ``async with`` block, or a failed
run) the slot is released and the child cleaned up exactly once (idempotent),
so an EPHEMERAL child can never leak its cap slot.
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Generic, Optional, TypeVar

from mote.contracts.agent import RunnableAgent
from mote.contracts.conversation import Message
from mote.contracts.output import RunOutcome
from mote.contracts.ports.agent.control import (
    ChildReleaseDisposition,
    ChildReleaseError,
    ChildReleasePort,
    ResidencyReservationPort,
)
from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime, AgentStatus, is_final
from mote.runtime.telemetry.logging import logger

OutputT = TypeVar("OutputT")


class ChildAgentHandle(Generic[OutputT]):
    """A spawned child agent + the means to run it and release its slot."""

    def __init__(
        self,
        runtime: AgentRuntime[OutputT],
        *,
        control: ChildReleasePort,
        agent_id: str,
        agent_path: Optional[AgentPath] = None,
        residency_slot: ResidencyReservationPort | None = None,
        poll_interval: float = 0.01,
        timeout_seconds: Optional[float] = None,
    ):
        self._runtime = runtime
        # ``control`` is the live AgentControl; held directly (the handle's
        # lifetime is strictly within a spawn site, shorter than the plane's).
        self._control = control
        self._agent_id = agent_id
        self._agent_path = agent_path
        # An EPHEMERAL child holds its (uncommitted, pending) residency slot here
        # so the live-incarnation slot is freed on aclose; MANAGED children commit
        # their slot in spawn_agent and pass ``None``.
        self._residency_slot = residency_slot
        self._poll_interval = poll_interval
        # EPHEMERAL wall-clock deadline for the single inline turn; ``None`` ==
        # unlimited. MANAGED children ignore this (their TTL is a control-plane
        # watchdog), so it is only ever consulted by ``run_to_completion``.
        self._timeout_seconds = timeout_seconds
        self._closed = False

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def runtime(self) -> AgentRuntime[OutputT]:
        return self._runtime

    @property
    def agent(self) -> RunnableAgent[OutputT]:
        """Contracts-owned execution view; orchestration runtime stays private."""
        return self._runtime.role

    @property
    def session_id(self) -> str:
        return self._agent_id

    @property
    def agent_path(self) -> Optional[AgentPath]:
        return self._agent_path

    @property
    def result(self) -> RunOutcome[OutputT] | None:
        """The child's typed result from its most recent completed run."""
        return self._runtime.last_run_result

    # ------------------------------------------------------------------
    # Drive shapes
    # ------------------------------------------------------------------
    async def run_to_completion(self, message: Message) -> RunOutcome[OutputT] | None:
        """Run the child inline for one turn, return its typed result, release it.

        The EPHEMERAL shape. The slot is always released (even on error) via the
        ``finally`` close.

        When the spawn carried a ``timeout_seconds`` the turn runs under an
        ``asyncio.wait_for`` deadline: on expiry the turn's ``CancelledError``
        path settles the runtime to INTERRUPTED and we return the partial summary
        (soft failure — a timed-out child yields whatever it produced, mirroring
        ``spawn_and_run`` degrading gracefully rather than raising).
        """
        try:
            if self._timeout_seconds is not None:
                try:
                    result = await asyncio.wait_for(self._runtime.run_one_turn(message), self._timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"ChildAgentHandle: {self._agent_id} exceeded its "
                        f"{self._timeout_seconds}s time budget; returning partial summary."
                    )
                    return None
            else:
                result = await self._runtime.run_one_turn(message)
            return result
        finally:
            await self.aclose()

    async def join(self) -> AgentStatus:
        """Wait for a MANAGED child (already in the scheduler) to reach a final status."""
        while not is_final(self._runtime.status):
            await asyncio.sleep(self._poll_interval)
        return self._runtime.status

    # ------------------------------------------------------------------
    # Teardown (idempotent)
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        """Release the cap slot and clean up the child role. Idempotent."""
        if self._closed:
            return
        receipt = await asyncio.shield(self._control.release_child(self._agent_id))
        if receipt.disposition in {
            ChildReleaseDisposition.SETTLED,
            ChildReleaseDisposition.ALREADY_TERMINAL,
        }:
            if self._residency_slot is not None:
                self._residency_slot.rollback()
            self._closed = True
            return
        raise ChildReleaseError(receipt)

    async def __aenter__(self) -> "ChildAgentHandle[OutputT]":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        await self.aclose()
        return False


__all__ = ["ChildAgentHandle"]
