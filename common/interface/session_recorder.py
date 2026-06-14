"""SessionRecorder protocol — the durable-log sink slice.

The narrow face ``ContextManager`` uses to stream the events it produces
(appended messages + compaction checkpoints) to a durable session log,
without importing the concrete ``session`` implementation.

Why a Protocol here (not in ``session``): ``context`` must never import
the ``session`` layer (the strict downward-only layering rule). The concrete
``SessionRecorder``/``SessionLog`` live in ``session`` and are *injected*
into ``ContextManager``; the manager only depends on this structural face, so
no upward import is introduced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from metagpt.common.schema import Message


@runtime_checkable
class SessionRecorder(Protocol):
    """The durable-log sink ``ContextManager`` writes its events to.

    Implemented by ``session.SessionRecorder`` (production) and any test
    double. Only the two events the manager emits are part of the contract; the
    sink is responsible for its own enable/disable, batching, and persistence.
    Calls must be cheap and non-throwing from the manager's point of view.
    """

    def record_message(self, message: "Message") -> None:
        """Persist a single appended message."""
        ...

    def record_compaction(self, messages: list["Message"], summary: str) -> None:
        """Persist a compaction checkpoint (the rebuilt history + its summary)."""
        ...
