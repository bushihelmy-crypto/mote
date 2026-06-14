"""Context management — microcompact, autocompact.

Ported from Claude Code's context-management stack. This package owns the
history-level scopes:

- Microcompact: fold old tool results (cheap, no LLM).
- Autocompact: summarize/rebuild the stored conversation when it nears the
  context window (expensive, LLM call).

The request-level scope (per-call compression before the wire request) lives in
``metagpt.router.llm.request_context_builder`` because it is tightly coupled to
BaseLLM.

The tool-level scope (per-tool result-size caps + disk persistence) lives in
``metagpt.executor.tool_result_limit``.

The ``ContextManager`` facade orchestrates the history scopes and owns the
stored conversation (replacing the old ``Memory`` object).
"""

from __future__ import annotations

from metagpt.common.schema import (
    AutocompactResult,
    ContextManagerConfig,
    MicrocompactResult,
    TokenState,
)
from metagpt.common.config.compress_msg_config import CompressType
from context import prompt, token_budget
from metagpt.context.autocompact import autocompact
from metagpt.context.manager import ContextManager
from metagpt.context.microcompact import (
    COMPACTABLE_TOOLS,
    microcompact,
)

__all__ = [
    "prompt",
    "token_budget",
    "TokenState",
    "CompressType",
    "ContextManagerConfig",
    "ContextManager",
    "COMPACTABLE_TOOLS",
    "MicrocompactResult",
    "microcompact",
    "AutocompactResult",
    "autocompact",
]
