"""Engine lifecycle boundary for shared Runtime resources and Agent instances."""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Callable, Generic, TypeVar

from mote.runtime.logging import logger
from mote.runtime.services import EngineServices

AgentT = TypeVar("AgentT")


class EngineState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class EngineShutdownError(RuntimeError):
    """Raised after shutdown attempted every resource and some closes failed."""

    def __init__(self, failures: list[BaseException]):
        self.failures = tuple(failures)
        details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
        super().__init__(f"Engine shutdown completed with {len(failures)} failure(s): {details}")


class Engine(Generic[AgentT]):
    """Own one shared Context and every Agent minted from its composition root.

    Shutdown is cancellation-safe and idempotent: the first caller starts one
    close task, later callers await the same task, and cancellation of an
    individual waiter cannot cancel resource cleanup.
    """

    def __init__(self, *, services: EngineServices, agent_factory: Callable[..., AgentT]) -> None:
        self.services = services
        self._agent_factory = agent_factory
        self._agents: dict[int, AgentT] = {}
        self._state = EngineState.OPEN
        self._close_task: asyncio.Task[None] | None = None
        self._ownership_lock = asyncio.Lock()

    @property
    def config(self):
        return self.services.context.config

    @property
    def state(self) -> EngineState:
        return self._state

    def agent(self, **kwargs: Any) -> AgentT:
        """Mint and register an Agent while the Engine is open."""

        if self._state is not EngineState.OPEN:
            raise RuntimeError(f"Engine is {self._state.value}; new Agents are not accepted.")
        agent = self._agent_factory(**kwargs)
        if agent is None:
            return agent
        self._agents[id(agent)] = agent
        return agent

    async def release(self, agent: AgentT) -> None:
        """Close one Agent and remove it from Engine ownership."""

        async with self._ownership_lock:
            if id(agent) not in self._agents:
                return
            cleanup = getattr(agent, "cleanup", None)
            if cleanup is not None:
                await cleanup()
            self._agents.pop(id(agent), None)

    async def __aenter__(self) -> "Engine[AgentT]":
        if self._state is not EngineState.OPEN:
            raise RuntimeError(f"Engine cannot be entered while {self._state.value}.")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            await self.aclose()
        else:
            try:
                await self.aclose()
            except Exception as close_exc:  # noqa: BLE001 — preserve body failure
                logger.warning(f"Engine shutdown failed while handling {type(exc).__name__}: {close_exc}")
        return False

    async def aclose(self) -> None:
        task = self._close_task
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(self._close(), name="mote-engine-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
        if self._state is EngineState.CLOSED:
            return
        self._state = EngineState.CLOSING
        failures: list[BaseException] = []
        async with self._ownership_lock:
            failed_agents: dict[int, AgentT] = {}
            for agent in reversed(self._agents.values()):
                cleanup = getattr(agent, "cleanup", None)
                if cleanup is None:
                    continue
                try:
                    await cleanup()
                except Exception as exc:  # retain failed owners for a later retry
                    failed_agents[id(agent)] = agent
                    failures.append(exc)
            self._agents = failed_agents
            if not failures:
                try:
                    await self.services.aclose()
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise EngineShutdownError(failures)
        self._state = EngineState.CLOSED


__all__ = ["Engine", "EngineShutdownError", "EngineState"]
