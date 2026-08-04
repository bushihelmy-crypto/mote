"""Tool execution and durability configuration models.

The enforcement logic lives in ``mote.runtime.resources.spill``; only the
pure-data config model and constants live here so any layer can reference them
without importing the executor package.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from mote.contracts.config.base import ConfigModel
from mote.contracts.tool.output_markers import PERSISTED_OUTPUT_CLOSE, PERSISTED_OUTPUT_OPEN

# ---------------------------------------------------------------------------
# Tool-level constants
# ---------------------------------------------------------------------------

# System-wide default cap on a single tool result, in characters. A tool may
# declare a lower (or higher) value via ``BaseTool.max_result_size_chars``.
DEFAULT_MAX_RESULT_SIZE_CHARS: int = 50_000

# Conservative bytes-per-token estimate.
BYTES_PER_TOKEN: int = 4

# When a tool result is persisted to disk, this many leading bytes are kept
# inline as a preview.
PREVIEW_SIZE_BYTES: int = 2_000

# XML-ish envelope wrapping a persisted tool result's inline preview. The literal
# is owned by the Runtime context marker authority; aliased here under
# the historical ``*_TAG`` names the executor already imports.
PERSISTED_OUTPUT_OPEN_TAG: str = PERSISTED_OUTPUT_OPEN
PERSISTED_OUTPUT_CLOSE_TAG: str = PERSISTED_OUTPUT_CLOSE

# Per-tool default caps on result size, in characters.
# Keyed by the Mote tool's primary name. Tools not listed fall back to
# DEFAULT_MAX_RESULT_SIZE_CHARS. (A tool can also override via its class attr.)
TOOL_MAX_RESULT_SIZE_CHARS: dict[str, int] = {
    "Read": 100_000,
    "Search": 100_000,
    "Bash": 30_000,
    "Edit": 100_000,
    "Sleep": 1_000,
}

# ---------------------------------------------------------------------------
# Output-compression constants (``mote.runtime.tools.compress``)
# ---------------------------------------------------------------------------

# Floor below which output is never compressed — small output is not worth the
# work and structural summaries can lose readability on tiny inputs.
COMPRESSION_MIN_OUTPUT_CHARS: int = 2_000

# Performance ceiling: output larger than this skips compression and falls back
# to the existing head-truncation/persistence path.
COMPRESSION_MAX_INPUT_CHARS: int = 2_000_000


class ToolResultLimitConfig(ConfigModel):
    """Knobs for per-tool result limiting (the tool-execution scope).

    Self-contained so :class:`ToolExecutor` needs no dependency on the
    higher-level context-manager config.
    """

    enable_tool_result_limit: bool = True
    persist_large_tool_results: bool = True
    default_max_result_size_chars: int = DEFAULT_MAX_RESULT_SIZE_CHARS
    preview_size_bytes: int = PREVIEW_SIZE_BYTES

    # Semantic output compression (git/pytest/ruff), applied before the size
    # cap. Fail-safe and default-on; disabling it reproduces prior behavior.
    enable_output_compression: bool = True
    compression_min_output_chars: int = COMPRESSION_MIN_OUTPUT_CHARS
    compression_max_input_chars: int = COMPRESSION_MAX_INPUT_CHARS


class ToolEffectStoreConfig(ConfigModel):
    """Knobs for the EXTERNAL-tool-effect idempotency ledger (crash-replay guard).

    Sibling of :class:`ToolResultLimitConfig`: a pure-data, tool-execution-scope
    policy the :class:`ToolExecutor` owns. When enabled, the executor records a
    durable started/completed/failed entry per EXTERNAL ``(session, tool_call_id)``
    around the tool body so a resume after a mid-call crash can tell a finished
    call from an in-flight one (and heal a dangling one from the recorded result
    instead of re-running its side effect). Disabling it reproduces the prior
    no-ledger behavior — every call simply runs.
    """

    enabled: bool = True


class LoopGuardConfig(ConfigModel):
    """Knobs for the tool-call loop guard (repeated-failure / no-progress detector).

    Sibling of :class:`ToolEffectStoreConfig`: a pure-data, tool-execution-scope
    policy the :class:`~mote.runtime.tools.tool_executor.ToolExecutor` wires as a
    ToolResultPolicy enrichment stage.
    It watches finished calls and, when a call thrashes, appends a nudge to that
    call's result steering the model to change approach or ask the user for help
    (via ``AskUserQuestion``) — a soft, in-band signal, never a hard block.

    Two orthogonal thrash shapes are counted per ``(tool_name, args-signature)``:

    - *repeated failure*: the SAME call (same args) fails ``failure_threshold``
      times in a row. A single success on that signature clears its count (real
      progress), so only an unbroken streak of identical failures trips it.
    - *no progress*: a PURE (read-only) call returns the SAME result
      ``no_progress_threshold`` times in a row. A read that keeps yielding the
      identical bytes is spinning, not observing fresh state.

    ``enabled=False`` leaves calls and results untouched.
    """

    enabled: bool = True

    # Consecutive identical-args failures of one tool before the guard nudges.
    # Counts a streak: a success on the same signature resets it to zero.
    failure_threshold: int = 3

    # Consecutive identical results from one PURE (read-only) call before the
    # guard flags it as making no progress. Only PURE tools are eligible — a
    # LOCAL/EXTERNAL tool legitimately repeats (a deploy loop, a poll).
    no_progress_threshold: int = 3


class ActivityConfig(ConfigModel):
    """Per-seam Temporal activity retry/timeout policy (Tier 2 only).

    One instance per durable seam (tool / think / timer): mote's three
    replay-safe seams become Temporal activities under ``backend="temporal"``,
    and each carries its own execution budget. Mirrors the fields pydantic-ai's
    Temporal integration exposes on its activity wrappers.

    All fields are plain data (seconds / ints / a string list) so this stays a
    pure config leaf — the optional ``runtime/durable/temporal`` adapter maps them
    onto ``temporalio``'s ``RetryPolicy`` / ``execute_activity`` kwargs at wire
    time, so the core never imports ``temporalio`` to hold this shape.

    - ``start_to_close_timeout_seconds``: the wall-clock ceiling for one activity
      attempt (the seam's body). ``None`` defers to the Temporal server default.
    - ``max_retry_attempts``: retry ceiling for a failed attempt (``0`` = unbounded,
      deferring to timeouts). Transient failures retry; a mote *user-logic* error
      (``ToolError`` / ``UserError``) is marked non-retryable by the seam wrapper
      so it fails fast rather than burning the retry budget (see ``B4``).
    - ``initial_retry_interval_seconds`` / ``retry_backoff_coefficient`` /
      ``max_retry_interval_seconds``: the retry backoff curve.
    - ``non_retryable_error_types``: extra fully-qualified error names the seam
      should treat as permanent (in addition to mote's user-logic errors).
    """

    start_to_close_timeout_seconds: Optional[float] = None
    max_retry_attempts: int = 0
    initial_retry_interval_seconds: float = 1.0
    retry_backoff_coefficient: float = 2.0
    max_retry_interval_seconds: Optional[float] = None
    non_retryable_error_types: list[str] = Field(default_factory=list)


class TemporalConfig(ConfigModel):
    """Connection and attempt policy for the Product-owned Workflow effect plane."""

    server_address: str = "localhost:7233"
    namespace: str = "default"
    task_queue: str = "mote"
    effect_activity: ActivityConfig = Field(default_factory=ActivityConfig)


class ToolSearchConfig(ConfigModel):
    """Master switch for the Tool Search subsystem (deferred-tool discovery).

    Sibling of :class:`ToolResultLimitConfig` / :class:`ToolEffectStoreConfig`: a
    pure-data, tool-execution-scope policy. Per-role ``RoleSchema.deferred_tools``
    declares WHICH tools are hidden-until-discovered; this ``enabled`` flag is the
    global OVERRIDE that gates whether that declaration takes effect at all.

    ``enabled=True`` (default) reproduces the existing behavior: a non-empty
    ``deferred_tools`` engages the machinery (SearchTools bound, compact menu
    injected, native ``defer_loading`` where the model supports it). Setting it
    ``False`` forces the effective deferred set to EMPTY for every role — so no
    tool is hidden, the ``SearchTools`` meta-tool is not bound, the deferred menu
    is not built, and no server-side tool-search path fires — every declared tool
    is simply fully visible on both channels (the plain no-deferral path).
    """

    enabled: bool = True
