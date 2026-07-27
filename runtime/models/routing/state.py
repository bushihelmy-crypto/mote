"""Role-owned implementation of the routing state port."""

from __future__ import annotations

from collections.abc import Callable

from mote.contracts.models.routing import RoutingSessionState


class RoleRoutingStateStore:
    def __init__(
        self,
        getter: Callable[[], RoutingSessionState],
        setter: Callable[[RoutingSessionState], None],
    ) -> None:
        self._getter = getter
        self._setter = setter

    async def read(self, session_id: str) -> RoutingSessionState:
        return self._getter()

    async def commit(
        self,
        session_id: str,
        *,
        expected_generation: int,
        state: RoutingSessionState,
    ) -> None:
        current = self._getter()
        if current.generation != expected_generation:
            raise RuntimeError(
                "routing state generation conflict: " f"expected {expected_generation}, found {current.generation}"
            )
        if state.generation != expected_generation + 1:
            raise ValueError("routing state commits must advance exactly one generation")
        self._setter(state)


__all__ = ["RoleRoutingStateStore"]
