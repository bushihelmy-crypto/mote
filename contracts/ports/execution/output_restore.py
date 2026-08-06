"""Narrow query for a restored terminal output owned by the output runtime."""

from __future__ import annotations

from typing import Protocol, TypeVar

from mote.contracts.execution.restore import CommittedExecution

OutputT = TypeVar("OutputT")


class CommittedExecutionQuery(Protocol[OutputT]):
    """Expose only the immutable terminal fact needed during run restore."""

    def restored_committed_execution(
        self,
    ) -> CommittedExecution[OutputT] | None: ...


__all__ = ["CommittedExecutionQuery"]
