"""Small, strongly typed public Agent facade."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

from mote.contracts.output import RunRejected, RunResult

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")
DriverDepsT_co = TypeVar("DriverDepsT_co", covariant=True)
DriverOutputT = TypeVar("DriverOutputT")


class AgentRunIncompleteError(RuntimeError):
    """The internal run ended without durably committing its output contract."""


class AgentRunRejectedError(RuntimeError):
    """The request was rejected before execution crossed an admission boundary."""

    def __init__(self, rejection: RunRejected) -> None:
        self.rejection = rejection
        super().__init__(rejection.reason)


class _AgentDriver(Protocol[DriverDepsT_co, DriverOutputT]):
    @property
    def name(self) -> str:
        ...

    @property
    def deps(self) -> DriverDepsT_co:
        ...

    async def run(self, with_message: str) -> RunResult[DriverOutputT] | RunRejected | None:
        ...


class Agent(Generic[DepsT, OutputT]):
    """Typed handle returned by :meth:`Engine.agent`.

    Users interact with dependencies, ``run`` and lifecycle only. Role schemas,
    component graphs, leases, routers, and provider clients remain behind this
    boundary.
    """

    __slots__ = ("_driver", "_release", "_is_open", "_closed")

    def __init__(self) -> None:
        raise TypeError("Agent instances are created by Engine.agent()")

    @classmethod
    def _create(
        cls,
        *,
        driver: _AgentDriver[DepsT, OutputT],
        release: Callable[[], Awaitable[None]],
        is_open: Callable[[], bool],
    ) -> "Agent[DepsT, OutputT]":
        agent = object.__new__(cls)
        agent._driver = driver
        agent._release = release
        agent._is_open = is_open
        agent._closed = False
        return agent

    @property
    def name(self) -> str:
        return self._driver.name

    @property
    def deps(self) -> DepsT:
        return self._driver.deps

    async def run(self, prompt: str) -> RunResult[OutputT]:
        if self._closed or not self._is_open():
            raise RuntimeError("Agent is closed and cannot start a run.")
        result = await self._driver.run(prompt)
        if isinstance(result, RunRejected):
            raise AgentRunRejectedError(result)
        if result is None:
            raise AgentRunIncompleteError(
                "Agent run ended without a committed output; inspect the run trace for its terminal condition."
            )
        return result

    async def aclose(self) -> None:
        if self._closed:
            return
        await self._release()
        self._closed = True

    async def __aenter__(self) -> "Agent[DepsT, OutputT]":
        if self._closed or not self._is_open():
            raise RuntimeError("Agent cannot be entered after it is closed.")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.aclose()
        return False


__all__ = ["Agent", "AgentRunIncompleteError", "AgentRunRejectedError"]
