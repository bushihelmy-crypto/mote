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


class InlineTelemetryHandle:
    def __init__(self, telemetry: InlineTelemetry, handler: Any) -> None:
        self._telemetry = telemetry
        self._handler = handler

    async def aclose(self) -> None:
        if self._handler in self._telemetry.handlers:
            self._telemetry.handlers.remove(self._handler)


__all__ = ["InlineTelemetry", "InlineTelemetryHandle"]
