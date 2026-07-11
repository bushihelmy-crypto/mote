"""Context management — the unified reduction pipeline.

This package owns the history-level scopes. The mechanics live in
``mote.context.compaction`` (a segmented :class:`Transcript` plus the
cheapest-first fold → summarize → drop reducer pipeline behind a
:class:`ContextEngine`); both the threshold-triggered (SOFT) and the reactive
context-overflow (HARD) reductions run through that one pipeline.

The tool-level scope (per-tool result-size caps + disk persistence) lives in
``mote.executor.tool_result_limit``.

The ``ContextManager`` facade orchestrates the history scopes and owns the
stored conversation (replacing the old ``Memory`` object).
"""

from __future__ import annotations

from mote.common.schema import ContextManagerConfig, TokenState
from mote.context import budget, prompt
from mote.context.manager import ContextManager
from mote.context.visibility import ContextVisibility

__all__ = [
    "prompt",
    "budget",
    "TokenState",
    "ContextManagerConfig",
    "ContextManager",
    "ContextVisibility",
]
