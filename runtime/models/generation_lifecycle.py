"""Ordered drain-before-close lifecycle for one model Runtime generation."""

from __future__ import annotations

import inspect
from collections.abc import Iterable


class GenerationLifecycle:
    def __init__(self, resources: Iterable[object], *, drain_timeout_seconds: float = 30.0) -> None:
        if drain_timeout_seconds <= 0:
            raise ValueError("generation drain timeout must be positive")
        unique: dict[int, object] = {}
        for resource in resources:
            unique.setdefault(id(resource), resource)
        self._resources = tuple(unique.values())
        self._drain_timeout_seconds = drain_timeout_seconds
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for resource in self._resources:
            drain = getattr(resource, "drain", None)
            if drain is None:
                continue
            try:
                result = drain(timeout_seconds=self._drain_timeout_seconds)
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(exc)
        for resource in self._resources:
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Runtime generation lifecycle failed", errors)


__all__ = ["GenerationLifecycle"]
