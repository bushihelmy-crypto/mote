"""Session-scoped opaque workflow continuation ownership."""

from __future__ import annotations

import uuid

from mote.orchestration.workflows import WorkflowContinuation, WorkflowRun


class AlreadyConsumed(LookupError):
    pass


class ResumeExpired(LookupError):
    pass


class WorkflowContinuationRegistry:
    def __init__(self, session_scope: str = "default") -> None:
        self._session_scope = session_scope
        self._continuations: dict[str, WorkflowContinuation] = {}
        self._consumed: set[str] = set()

    def register(self, continuation: WorkflowContinuation) -> str:
        ref = f"wfr_1_{uuid.uuid4().hex}"
        self._continuations[ref] = continuation
        return ref

    def consume(
        self,
        ref: str,
        overrides: dict | None = None,
        *,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
    ) -> WorkflowRun:
        if ref in self._consumed:
            raise AlreadyConsumed(ref)
        continuation = self._continuations.pop(ref, None)
        if continuation is None:
            raise ResumeExpired(ref)
        self._consumed.add(ref)
        return continuation.resume(
            overrides,
            from_nodes=from_nodes,
            skip_nodes=skip_nodes,
        )

    def resume(
        self,
        ref: str,
        overrides: dict | None = None,
        *,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
    ) -> WorkflowRun:
        return self.consume(
            ref,
            overrides,
            from_nodes=from_nodes,
            skip_nodes=skip_nodes,
        )

    def discard(self, ref: str) -> None:
        self._continuations.pop(ref, None)

    async def aclose(self) -> None:
        self._continuations.clear()


__all__ = [
    "AlreadyConsumed",
    "ResumeExpired",
    "WorkflowContinuationRegistry",
]
