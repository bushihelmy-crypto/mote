"""Task-local access to the Runtime composition lease of the active turn."""

from __future__ import annotations

from contextvars import ContextVar, Token

from mote.contracts.model.topology import RouteId


class RuntimeCompositionScopeError(RuntimeError):
    pass


_CURRENT_RUNTIME_COMPOSITION: ContextVar[object | None] = ContextVar("mote_runtime_composition", default=None)


def bind_runtime_composition(lease: object) -> Token:
    return _CURRENT_RUNTIME_COMPOSITION.set(lease)


def reset_runtime_composition(token: Token) -> None:
    _CURRENT_RUNTIME_COMPOSITION.reset(token)


def current_runtime_composition():
    lease = _CURRENT_RUNTIME_COMPOSITION.get()
    if lease is None:
        raise RuntimeCompositionScopeError("model access requires an active application turn lease")
    return lease


class CurrentRuntimeModelGateway:
    """Gateway proxy that borrows the active turn lease without acquiring."""

    @staticmethod
    def _gateway():
        return current_runtime_composition().gateway

    def supports_route(self, route_id: RouteId) -> bool:
        return self._gateway().supports_route(route_id)

    def route_profile(self, route_id: RouteId):
        return self._gateway().route_profile(route_id)

    def route_profiles(self, route_id: RouteId):
        return self._gateway().route_profiles(route_id)

    async def execute(self, invocation, **kwargs):
        return await self._gateway().execute(invocation, **kwargs)

    async def resume(self, invocation, **kwargs):
        return await self._gateway().resume(invocation, **kwargs)


__all__ = [
    "CurrentRuntimeModelGateway",
    "RuntimeCompositionScopeError",
    "bind_runtime_composition",
    "current_runtime_composition",
    "reset_runtime_composition",
]
