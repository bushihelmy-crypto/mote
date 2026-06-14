"""Constants for context management — ported from Claude Code.

Two scopes of limits live here:

1. History-level — autocompact token thresholds/buffers and the placeholder
   text used by microcompact. Ported from CC ``services/compact/autoCompact.ts``
   and ``microCompact.ts``.
2. Request-level — handled by the migrated request-compress strategy; its own
   constants stay with that module.

The third (tool-level) scope — per-tool result-size caps and disk-persistence
preview size — is NOT here: it lives in ``metagpt.executor.tool_result_limit``
because it is a tool-execution concern. Keeping those constants in the executor
layer means this module never imports ``executor`` (dependency points downward,
``context`` → ``executor``).

Values are kept close to CC so behavior is comparable; where CC scales by the
model's context window we expose helpers in ``token_budget.py`` instead of
hard-coding.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# History-level (microcompact — old tool-result folding)
# ---------------------------------------------------------------------------

# Placeholder that replaces an old tool-result's content once folded away
# (CC ``TOOL_RESULT_CLEARED_MESSAGE`` / ``TIME_BASED_MC_CLEARED_MESSAGE``).
TOOL_RESULT_CLEARED_MESSAGE: str = "[Old tool result content cleared]"

# How many of the most-recent tool results microcompact always keeps intact
# (CC ``KEEP_RECENT``).
MICROCOMPACT_KEEP_RECENT: int = 5

# Only fold once at least this many tool results have accumulated
# (CC cached-microcompact ``TRIGGER_THRESHOLD``).
MICROCOMPACT_TRIGGER_THRESHOLD: int = 10

# ---------------------------------------------------------------------------
# History-level (autocompact — summarize & rebuild)
# ---------------------------------------------------------------------------

# Tokens reserved for the summary's own completion when computing the effective
# window (CC ``MAX_OUTPUT_TOKENS_FOR_SUMMARY`` / ``COMPACT_MAX_OUTPUT_TOKENS``).
MAX_OUTPUT_TOKENS_FOR_SUMMARY: int = 20_000

# Safety buffer below the effective window at which autocompact fires. CC
# scales this with the window (13k / 30k / 50k); see ``autocompact_buffer``.
AUTOCOMPACT_BUFFER_TOKENS: int = 13_000

# Buffers (below threshold) for the warning / error / blocking UI states
# (CC ``WARNING_THRESHOLD_BUFFER_TOKENS`` etc.).
WARNING_THRESHOLD_BUFFER_TOKENS: int = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS: int = 20_000
MANUAL_COMPACT_BUFFER_TOKENS: int = 3_000

# Give up autocompacting after this many consecutive failures in one session
# (CC ``MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES``).
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES: int = 3

# Default model context window when a model is unknown to ``TOKEN_MAX``
# (CC ``MODEL_CONTEXT_WINDOW_DEFAULT``).
MODEL_CONTEXT_WINDOW_DEFAULT: int = 200_000

# When autocompacting, keep at least this many recent tokens / messages of the
# tail verbatim (the part not summarized). Mirrors CC session-memory bounds.
AUTOCOMPACT_KEEP_TAIL_TOKENS: int = 10_000
AUTOCOMPACT_KEEP_TAIL_MESSAGES: int = 5
