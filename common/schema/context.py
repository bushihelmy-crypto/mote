"""Context-manager result types and config — consolidated from context/.

Contains the pure-data models that context modules produce/consume.
The algorithmic logic stays in its respective module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from metagpt.common.config.config.compress_msg_config import CompressType
from metagpt.common.const.context import (
    AUTOCOMPACT_BUFFER_TOKENS as _AUTOCOMPACT_BUFFER_TOKENS,
    AUTOCOMPACT_KEEP_TAIL_MESSAGES as _AUTOCOMPACT_KEEP_TAIL_MESSAGES,
    AUTOCOMPACT_KEEP_TAIL_TOKENS as _AUTOCOMPACT_KEEP_TAIL_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES as _MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY as _MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    MICROCOMPACT_KEEP_RECENT as _MICROCOMPACT_KEEP_RECENT,
    MICROCOMPACT_TRIGGER_THRESHOLD as _MICROCOMPACT_TRIGGER_THRESHOLD,
)
from metagpt.common.schema.messages import Message


# ---------------------------------------------------------------------------
# ContextManagerConfig (from context/config.py)
# ---------------------------------------------------------------------------


class ContextManagerConfig(BaseModel):
    """Tunable knobs for the context manager's own scopes (history + request).

    The tool-execution scope (per-tool result caps + disk persistence) is NOT
    here — it is owned by ``ToolResultLimitConfig`` and configured on the
    ``ToolExecutor`` directly.
    """

    # --- History-level: microcompact (fold old tool results) ---
    enable_microcompact: bool = True
    microcompact_keep_recent: int = _MICROCOMPACT_KEEP_RECENT
    microcompact_trigger_threshold: int = _MICROCOMPACT_TRIGGER_THRESHOLD

    # --- History-level: autocompact (summarize & rebuild) ---
    enable_autocompact: bool = True
    autocompact_buffer_tokens: int = _AUTOCOMPACT_BUFFER_TOKENS
    max_output_tokens_for_summary: int = _MAX_OUTPUT_TOKENS_FOR_SUMMARY
    keep_tail_tokens: int = _AUTOCOMPACT_KEEP_TAIL_TOKENS
    keep_tail_messages: int = _AUTOCOMPACT_KEEP_TAIL_MESSAGES
    max_consecutive_failures: int = _MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES

    # --- Request-level: per-call message compression ---
    compress_type: CompressType = CompressType.NO_COMPRESS
    request_compress_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# TokenState (from context/token_budget.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenState:
    """A snapshot of where the conversation sits relative to the window.

    Mirrors CC ``calculateTokenWarningState`` plus the autocompact decision.
    """

    token_count: int
    model: str
    effective_window: int
    autocompact_threshold: int
    percent_left: int
    above_warning: bool
    above_error: bool
    above_autocompact: bool
    at_blocking_limit: bool

    @property
    def should_autocompact(self) -> bool:
        """True when the stored history should be summarized & rebuilt."""
        return self.above_autocompact


# ---------------------------------------------------------------------------
# MicrocompactResult (from context/microcompact.py)
# ---------------------------------------------------------------------------


@dataclass
class MicrocompactResult:
    """Outcome of a microcompact pass.

    ``messages`` is the same list that was passed in (folding is done in place);
    it is returned for symmetry with the other context strategies. ``changed``
    is True only when at least one tool result was folded.
    """

    messages: list[Message]
    tokens_freed: int = 0
    cleared_tool_use_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.cleared_tool_use_ids)


# ---------------------------------------------------------------------------
# AutocompactResult (from context/autocompact.py)
# ---------------------------------------------------------------------------


@dataclass
class AutocompactResult:
    """Outcome of an autocompact attempt.

    ``messages`` is the rebuilt history (``[summary] + tail``) when compaction
    ran, otherwise the original list unchanged. ``compacted`` is False both when
    the threshold wasn't reached and when the summarize call failed.
    """

    messages: list[Message]
    compacted: bool = False
    summary: Optional[str] = None
    pre_compact_tokens: int = 0
    post_compact_tokens: int = 0
    consecutive_failures: int = 0
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return self.compacted
