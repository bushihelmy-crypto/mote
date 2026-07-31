"""Engine-owned Langfuse client activation and exporter lifecycle."""

from __future__ import annotations

import inspect
from typing import Any

try:
    from langfuse import Langfuse
except Exception:  # noqa: BLE001 — optional backend may have incompatible transitive deps
    Langfuse = None

from mote.runtime.config.langfuse import LangfuseConfig
from mote.runtime.telemetry.logging import logger
from mote.runtime.telemetry.observability.langfuse_backend import LangfuseBackend
from mote.runtime.telemetry.observability.tracing import TracingSubscriber


class LangfuseRuntime:
    """One Engine's isolated Langfuse client and subscriber factory."""

    def __init__(self, client: Any = None, *, trace_steps: bool = False) -> None:
        self._client = client
        self._trace_steps = trace_steps
        self._closed = False

    @classmethod
    def from_config(cls, config: LangfuseConfig) -> "LangfuseRuntime":
        if not config.enabled:
            return cls()
        if not (config.public_key and config.secret_key):
            logger.warning("Langfuse enabled but public_key/secret_key missing; tracing stays disabled.")
            return cls()
        if Langfuse is None:
            logger.warning("Langfuse enabled but its optional dependency is unavailable or incompatible.")
            return cls()
        try:
            client = Langfuse(
                public_key=config.public_key,
                secret_key=config.secret_key,
                host=config.host,
                sample_rate=config.sample_rate,
            )
        except Exception as exc:  # noqa: BLE001 — observability cannot block Runtime startup
            logger.warning(f"Langfuse client init failed ({exc!r}); tracing stays disabled.")
            return cls()
        return cls(client, trace_steps=config.trace_steps)

    @property
    def enabled(self) -> bool:
        return self._client is not None and not self._closed

    def subscriber(self) -> TracingSubscriber | None:
        if not self.enabled:
            return None
        return TracingSubscriber(
            LangfuseBackend(client_factory=lambda: self._client),
            trace_steps=self._trace_steps,
        )

    async def aclose(self) -> None:
        """Flush buffered spans, then release the Engine-owned SDK client."""

        if self._closed:
            return
        client = self._client
        if client is None:
            self._closed = True
            return
        flush = getattr(client, "flush", None)
        if flush is not None:
            result = flush()
            if inspect.isawaitable(result):
                await result
        shutdown = getattr(client, "shutdown", None) or getattr(client, "close", None)
        if shutdown is not None:
            result = shutdown()
            if inspect.isawaitable(result):
                await result
        self._client = None
        self._closed = True


__all__ = ["LangfuseRuntime"]
