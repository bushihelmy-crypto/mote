"""Stable defaults and markers for context management.

Two scopes of limits live here:

1. History-level — autocompact token thresholds/buffers and the placeholder
   text used by microcompact.
2. Request-level — handled by the request-compress strategy; its own
   constants stay with that module.

Tool-execution limits remain Runtime concerns; only cross-layer data defaults
and durable message markers belong here.

Where a limit scales by the model's context window, Runtime exposes helpers
instead of hard-coding policy in Contracts.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# History-level (microcompact — old tool-result folding)
# ---------------------------------------------------------------------------

# Placeholder that replaces an old tool-result's content once folded away.
TOOL_RESULT_CLEARED_MESSAGE: str = "[Old tool result content cleared]"

# Neutral marker that REPLACES an old Edit whole-file write's ``new_string``
# once the fold reducer clears it (the arguments twin of
# ``TOOL_RESULT_CLEARED_MESSAGE`` for a tool RESULT). It reads in the SYSTEM's
# voice — "the environment folded this value away" — never as the model's own
# input, so re-reading the call after the fold does NOT look like "I typed a
# placeholder into the file". It points at the paired result and at the on-disk
# file for exact current content. It deliberately asserts nothing about success and names no specific
# result section.
FOLDED_WRITE_MARKER: str = (
    "[folded: the full file content of this write is omitted from the recorded "
    "arguments to save context. Its outcome is in the paired tool result; Read "
    "the file for its exact current content.]"
)

# Marker prepended in place of the oldest turns the (destructive) head-drop
# reducer irreversibly discarded when nothing cheaper could free enough room.
HEAD_DROPPED_MESSAGE: str = "[earlier turns truncated to fit the context window]"

# How many recent model tool-call turns microcompact keeps intact. One turn is
# one assistant thinking response, its batch of tool calls, and their results.
MICROCOMPACT_KEEP_RECENT: int = 5

# Do not start folding until more than ten reconstructable turns are live. This
# accumulation gate is independent from the five-turn protected tail above.
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
