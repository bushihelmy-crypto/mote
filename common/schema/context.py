"""Context-manager result types and config — consolidated from context/.

Contains the pure-data models that context modules produce/consume.
The algorithmic logic stays in its respective module.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from metagpt.common.const.context import (
    AUTOCOMPACT_BUFFER_TOKENS as _AUTOCOMPACT_BUFFER_TOKENS,
    AUTOCOMPACT_KEEP_TAIL_MESSAGES as _AUTOCOMPACT_KEEP_TAIL_MESSAGES,
    AUTOCOMPACT_KEEP_TAIL_TOKENS as _AUTOCOMPACT_KEEP_TAIL_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES as _MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY as _MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    MICROCOMPACT_CLEAR_AT_LEAST_TOKENS as _MICROCOMPACT_CLEAR_AT_LEAST_TOKENS,
    MICROCOMPACT_KEEP_RECENT as _MICROCOMPACT_KEEP_RECENT,
    MICROCOMPACT_TRIGGER_THRESHOLD as _MICROCOMPACT_TRIGGER_THRESHOLD,
)


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
    # Min tokens a fold must free to be worth the prompt-cache write it forces
    # (mirrors Anthropic context-editing's ``clear_at_least``). Below this the
    # fold is skipped so a trivial trim never eats a cache miss.
    microcompact_clear_at_least: int = _MICROCOMPACT_CLEAR_AT_LEAST_TOKENS

    # --- History-level: autocompact (summarize & rebuild) ---
    enable_autocompact: bool = True
    autocompact_buffer_tokens: int = _AUTOCOMPACT_BUFFER_TOKENS
    max_output_tokens_for_summary: int = _MAX_OUTPUT_TOKENS_FOR_SUMMARY
    keep_tail_tokens: int = _AUTOCOMPACT_KEEP_TAIL_TOKENS
    keep_tail_messages: int = _AUTOCOMPACT_KEEP_TAIL_MESSAGES
    max_consecutive_failures: int = _MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES


# ---------------------------------------------------------------------------
# TokenState (from context/budget.py)
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
