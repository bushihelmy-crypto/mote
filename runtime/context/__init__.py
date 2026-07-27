"""Context management — the unified reduction pipeline.

This package owns the history-level scopes. The mechanics live in
``mote.runtime.context.compaction`` (a segmented :class:`Transcript` plus the
cheapest-first fold → summarize → drop reducer pipeline behind a
:class:`ContextEngine`); both the threshold-triggered (SOFT) and the reactive
context-overflow (HARD) reductions run through that one pipeline.

The tool-level scope (per-tool result-size caps + disk persistence) lives in
``mote.runtime.tools.tool_result_limit``.

The ``ContextManager`` facade orchestrates the history scopes and owns the
stored conversation.
"""

from __future__ import annotations

from mote.contracts.schema import ContextManagerConfig, TokenState
from mote.runtime.context import budget, prompt
from mote.runtime.context.manager import ContextManager
from mote.runtime.context.visibility import ContextVisibility

__all__ = [
    "prompt",
    "budget",
    "TokenState",
    "ContextManagerConfig",
    "ContextManager",
    "ContextVisibility",
]
