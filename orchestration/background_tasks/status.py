"""Unified status enum for background tasks and graph nodes.

This is a LEAF module (stdlib only), safe to import from anywhere without
risking circular imports.
"""

from __future__ import annotations

from enum import Enum


class BgStatus(str, Enum):
    """Unified status values for background tasks and graph nodes."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    WAITING_FOR_ROUTE = "waiting_for_route"
    STALLED = "stalled"


# Statuses where a task paused mid-run awaiting a model decision — resumable,
# NOT terminal. A pause keeps its state snapshot for ``resume_tasks`` and stays
# cancellable. Two reasons share this shape (see ``bggraph.types.PauseReason``):
# ``WAITING_FOR_ROUTE`` (frontier hit an LLM edge — pick a route) and
# ``STALLED`` (frontier drained with a blocked AND-join — a deadlock the model
# must break). Keeping the set here (the leaf status module) lets the resume /
# cancel gates test "is this a resumable pause?" from one authoritative place
# instead of re-listing the members.
PAUSE_STATUSES = frozenset({BgStatus.WAITING_FOR_ROUTE, BgStatus.STALLED})

# Whole-task *terminal* statuses — a task that has genuinely finished (as opposed
# to a resumable pause). Single authoritative source so the attachment generator,
# the push-once result registration, and any reap gate all agree on "done".
TERMINAL_STATUSES = frozenset({BgStatus.SUCCESS, BgStatus.FAILED, BgStatus.CANCELLED, BgStatus.TIMEOUT})
