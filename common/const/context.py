"""Constants for context management.

Two scopes of limits live here:

1. History-level — autocompact token thresholds/buffers and the placeholder
   text used by microcompact.
2. Request-level — handled by the request-compress strategy; its own
   constants stay with that module.

The third (tool-level) scope — per-tool result-size caps and disk-persistence
preview size — is NOT here: it lives in ``mote.executor.tool_result_limit``
because it is a tool-execution concern. Keeping those constants in the executor
layer means this module never imports ``executor`` (dependency points downward,
``context`` → ``executor``).

Where a limit scales by the model's context window we expose helpers in
``budget.py`` instead of hard-coding.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# History-level (microcompact — old tool-result folding)
# ---------------------------------------------------------------------------

# Placeholder that replaces an old tool-result's content once folded away.
TOOL_RESULT_CLEARED_MESSAGE: str = "[Old tool result content cleared]"

# Marker prepended in place of the oldest turns the (destructive) head-drop
# reducer irreversibly discarded when nothing cheaper could free enough room.
HEAD_DROPPED_MESSAGE: str = "[earlier turns truncated to fit the context window]"

# How many of the most-recent tool results microcompact always keeps intact.
MICROCOMPACT_KEEP_RECENT: int = 5

# Only fold once at least this many tool results have accumulated.
MICROCOMPACT_TRIGGER_THRESHOLD: int = 10

# Minimum tokens a fold pass must free to be worth doing. Folding rewrites the
# content of old messages, which changes the request prefix and forces a
# one-time prompt-cache write (Anthropic caching is a strict prefix match); the
# saving only pays off once amortized over later turns. So skip a fold that
# would free less than this — never eat a cache miss for a trivial trim. Mirrors
# Anthropic context-editing's ``clear_at_least``.
MICROCOMPACT_CLEAR_AT_LEAST_TOKENS: int = 5_000

# ---------------------------------------------------------------------------
# History-level (autocompact — summarize & rebuild)
# ---------------------------------------------------------------------------

# Tokens reserved for the summary's own completion when computing the effective
# window.
MAX_OUTPUT_TOKENS_FOR_SUMMARY: int = 20_000

# Safety buffer below the effective window at which autocompact fires. This
# scales with the window (13k / 30k / 50k); see ``autocompact_buffer``.
AUTOCOMPACT_BUFFER_TOKENS: int = 13_000

# Buffers (below threshold) for the warning / error / blocking UI states.
WARNING_THRESHOLD_BUFFER_TOKENS: int = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS: int = 20_000
MANUAL_COMPACT_BUFFER_TOKENS: int = 3_000

# Give up autocompacting after this many consecutive failures in one session.
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES: int = 3

# Default model context window when a model is unknown to ``TOKEN_MAX``.
MODEL_CONTEXT_WINDOW_DEFAULT: int = 200_000

# When autocompacting, keep at least this many recent tokens / messages of the
# tail verbatim (the part not summarized).
AUTOCOMPACT_KEEP_TAIL_TOKENS: int = 10_000
AUTOCOMPACT_KEEP_TAIL_MESSAGES: int = 5

# ---------------------------------------------------------------------------
# History-level (post-compact file rehydration)
# ---------------------------------------------------------------------------

# After a summarize compaction discards the head, re-read the files the session
# most-recently touched and re-inject their *current* bytes right after the
# summary, so the model keeps a fresh view of its working set instead of relying
# on the summary's prose recollection.
# Budgets cap file attachments: at most this many files, each
# truncated (head-kept) to PER_FILE tokens, added most-recent-first until the
# running total would exceed the overall budget.
POST_COMPACT_REHYDRATE_MAX_FILES: int = 5
POST_COMPACT_REHYDRATE_MAX_TOKENS_PER_FILE: int = 10_000
POST_COMPACT_REHYDRATE_TOKEN_BUDGET: int = 50_000
