"""LangfuseBackend — the only SDK-touching tracer backend.

Implements :class:`~mote.runtime.observability.tracing.TracerBackend` against
the langfuse v3 client. Nesting is **explicit-parent**: a child observation is
created off its parent *handle* (``parent_handle.start_observation(...)``) when
one is present, else off the client — never from langfuse's ambient contextvar.
The trace tree is therefore driven entirely by the IDs the spine carries. The
Engine-owned SDK client is constructed by ``langfuse_integration`` and injected
here; this adapter never resolves process-global SDK state.

Every call is best-effort (try/except → silent no-op): langfuse may be
uninstalled, the client may fail to resolve, or the v3 handle-child API may
drift — none of which may ever break a turn. Returned handles are opaque to
:class:`TracingSubscriber`; ``None`` is a valid handle (degrades to no-op).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mote.runtime.logging import logger


class LangfuseBackend:
    """Maps the backend-agnostic tracer calls onto langfuse observations."""

    def __init__(self, client_factory: Callable[[], Any]):
        self._client_factory = client_factory

    def _client(self) -> Any:
        return self._client_factory()

    # ------------------------------------------------------------------ spans
    def start_span(
        self,
        *,
        span_id: str,
        parent_handle: Any,
        trace_id: str,
        label: str,
        attributes: dict,
    ) -> Any:
        try:
            if parent_handle is not None:
                handle = parent_handle.start_observation(as_type="span", name=label)
            else:
                client = self._client()
                handle = client.start_observation(as_type="span", name=label)
                # Root span: propagate the session id onto the langfuse trace.
                try:
                    client.update_current_trace(session_id=trace_id)
                except Exception as exc:  # noqa: BLE001 — propagation is best-effort
                    logger.debug(f"LangfuseBackend: trace session-id propagation failed: {exc}")
            if attributes:
                try:
                    handle.update(input=attributes)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"LangfuseBackend: span input attributes update failed: {exc}")
            return handle
        except Exception as exc:  # noqa: BLE001 — observability never breaks a turn
            logger.debug(f"LangfuseBackend.start_span failed: {exc}")
            return None

    def end_span(self, handle: Any, *, status: str, error: str, attributes: dict) -> None:
        if handle is None:
            return
        try:
            if status == "error":
                handle.update(level="ERROR", status_message=error)
            handle.end()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"LangfuseBackend.end_span failed: {exc}")

    # ------------------------------------------------------------ generations
    def start_generation(
        self,
        *,
        request_id: str,
        parent_handle: Any,
        trace_id: str,
        model: str,
        input: Any,
        metadata: dict,
    ) -> Any:
        try:
            kwargs = dict(as_type="generation", name=f"llm:{model}", model=model, input=input, metadata=metadata)
            if parent_handle is not None:
                return parent_handle.start_observation(**kwargs)
            return self._client().start_observation(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"LangfuseBackend.start_generation failed: {exc}")
            return None

    def update_generation(
        self,
        handle: Any,
        *,
        output: Any = None,
        usage: Any = None,
        metadata: Optional[dict] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
    ) -> None:
        if handle is None:
            return
        try:
            kwargs: dict = {}
            if output is not None:
                kwargs["output"] = output
            if usage is not None:
                kwargs["usage"] = usage
            if metadata is not None:
                kwargs["metadata"] = metadata
            if level is not None:
                kwargs["level"] = level
            if status_message is not None:
                kwargs["status_message"] = status_message
            handle.update(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"LangfuseBackend.update_generation failed: {exc}")

    def end_generation(self, handle: Any) -> None:
        if handle is None:
            return
        try:
            handle.end()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"LangfuseBackend.end_generation failed: {exc}")


__all__ = ["LangfuseBackend"]
