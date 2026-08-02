"""Small inline test double for producer-focused telemetry unit tests."""

from __future__ import annotations

from typing import Any


class InlineTelemetry:
    def __init__(self, *handlers: Any) -> None:
        self.handlers = list(handlers)

    async def emit(self, event: object) -> None:
        for handler in tuple(self.handlers):
            await handler.handle(event)

    def emit_sync(self, event: object) -> None:
        for handler in tuple(self.handlers):
            handle_sync = getattr(handler, "handle_sync", None)
            if handle_sync is not None:
                handle_sync(event)

    async def subscribe(self, binding: Any) -> "InlineTelemetryHandle":
        self.handlers.append(binding.handler)
        return InlineTelemetryHandle(self, binding.handler)

    async def subscribe_typed(
        self,
        _spec: object,
        event_type: type,
        handler: object,
        sync_handler: object | None = None,
    ) -> "InlineTelemetryHandle":
        binding = _TypedInlineHandler(event_type, handler, sync_handler)
        self.handlers.append(binding)
        return InlineTelemetryHandle(self, binding)


class _TypedInlineHandler:
    def __init__(self, event_type: type, handler: object, sync_handler: object | None) -> None:
        self._event_type = event_type
        self._handler = handler
        self._sync_handler = sync_handler

    async def handle(self, event: object) -> None:
        if type(event) is self._event_type:
            await self._handler.handle(event)

    def handle_sync(self, event: object) -> None:
        if type(event) is self._event_type and self._sync_handler is not None:
            self._sync_handler.handle_sync(event)


class InlineTelemetryHandle:
    def __init__(self, telemetry: InlineTelemetry, handler: Any) -> None:
        self._telemetry = telemetry
        self._handler = handler

    async def aclose(self) -> None:
        if self._handler in self._telemetry.handlers:
            self._telemetry.handlers.remove(self._handler)


__all__ = ["InlineTelemetry", "InlineTelemetryHandle"]
