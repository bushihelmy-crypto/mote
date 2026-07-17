#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ChildAgentHandle — the one shape every spawn site funnels through.

A handle is what :meth:`AgentControl.spawn_agent` hands back: a thin RAII wrapper
around the spawned :class:`AgentRuntime` + its committed
:class:`SpawnReservation`, so the four hand-written *construct → run → read →
cleanup* sites collapse into a single, consistent object.

Two drive shapes mirror :class:`~mote.common.agent_control.Lifecycle`:
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
from typing import Any, Optional

from mote.common.logs import logger
from mote.environment.agent_path import AgentPath
from mote.environment.runtime import AgentRuntime, AgentStatus, is_final


class ChildAgentHandle:
    """A spawned child agent + the means to run it and release its slot."""

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        control: Any,
        agent_id: str,
        agent_path: Optional[AgentPath] = None,
        residency_slot: Optional[Any] = None,
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
    def runtime(self) -> AgentRuntime:
        return self._runtime

    @property
    def session_id(self) -> str:
        return self._agent_id

    @property
    def agent_path(self) -> Optional[AgentPath]:
        return self._agent_path

    @property
    def result(self) -> str:
        """The child's terminal summary (``state.last_end_output``)."""
        state = getattr(self._runtime.role, "state", None)
        return (getattr(state, "last_end_output", "") or "").strip()

    # ------------------------------------------------------------------
    # Drive shapes
    # ------------------------------------------------------------------
    async def run_to_completion(self, message: Any) -> str:
        """Run the child inline for one turn, read its summary, release it.

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
                    await asyncio.wait_for(self._runtime.run_one_turn(message), self._timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"ChildAgentHandle: {self._agent_id} exceeded its "
                        f"{self._timeout_seconds}s time budget; returning partial summary."
                    )
            else:
                await self._runtime.run_one_turn(message)
            return self.result
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
        self._closed = True
        # Free the live-incarnation slot first (idempotent; no-op once committed
        # or rolled back) so the cap frees even on the exception path.
        if self._residency_slot is not None:
            try:
                self._residency_slot.rollback()
            except Exception as exc:  # noqa: BLE001 — slot release is best-effort
                logger.warning(f"ChildAgentHandle: slot release of {self._agent_id} failed: {exc}")
        # Release the registry slot next so the cap frees even if cleanup hangs.
        try:
            self._control.release_child(self._agent_id)
        except Exception as exc:  # noqa: BLE001 — release is best-effort
            logger.warning(f"ChildAgentHandle: release of {self._agent_id} failed: {exc}")
        cleanup = getattr(self._runtime.role, "cleanup", None)
        if cleanup is not None:
            try:
                await cleanup()
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                logger.warning(f"ChildAgentHandle: cleanup of {self._agent_id} failed: {exc}")

    async def __aenter__(self) -> "ChildAgentHandle":
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.aclose()
        return False


__all__ = ["ChildAgentHandle"]
