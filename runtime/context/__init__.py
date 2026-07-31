"""Context management — the unified reduction pipeline.

This package owns the history-level scopes. The mechanics live in
``mote.runtime.context.compaction`` (a segmented :class:`Transcript` plus the
cheapest-first fold → summarize → drop reducer pipeline behind a
:class:`ContextEngine`); both the threshold-triggered (SOFT) and the reactive
context-overflow (HARD) reductions run through that one pipeline.

The tool-level scope (per-tool result-size caps + disk persistence) lives in
``mote.runtime.resources.spill``.

The ``ContextManager`` facade orchestrates the history scopes and owns the
stored conversation.
"""

from __future__ import annotations

from mote.contracts.conversation import ContextManagerConfig, TokenState
from mote.runtime.context.history.manager import ContextManager
from mote.runtime.context.history.visibility import ContextVisibility

__all__ = [
    "TokenState",
    "ContextManagerConfig",
    "ContextManager",
    "ContextVisibility",
]
