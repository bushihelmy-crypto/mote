"""Tool execution config and constants — consolidated from mote.executor/tool_result_limit.py.

The enforcement logic stays in ``mote.executor.tool_result_limit``; only the
pure-data config model and constants live here so any layer can reference them
without importing the executor package.
"""

from __future__ import annotations

from pydantic import BaseModel

from mote.common.text import PERSISTED_OUTPUT_CLOSE, PERSISTED_OUTPUT_OPEN

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
# is owned by the marker authority (``common/text/markers.py``); aliased here under
# the historical ``*_TAG`` names the executor already imports.
PERSISTED_OUTPUT_OPEN_TAG: str = PERSISTED_OUTPUT_OPEN
PERSISTED_OUTPUT_CLOSE_TAG: str = PERSISTED_OUTPUT_CLOSE

# Per-tool default caps on result size, in characters.
# Keyed by the Mote tool's primary name. Tools not listed fall back to
# DEFAULT_MAX_RESULT_SIZE_CHARS. (A tool can also override via its class attr.)
TOOL_MAX_RESULT_SIZE_CHARS: dict[str, int] = {
    "Read": 100_000,
    "Glob": 100_000,
    "Grep": 20_000,
    "Bash": 30_000,
    "Edit": 100_000,
    "Write": 100_000,
    "Sleep": 1_000,
}

# ---------------------------------------------------------------------------
# Output-compression constants (``mote.executor.compress``)
# ---------------------------------------------------------------------------

# Floor below which output is never compressed — small output is not worth the
# work and structural summaries can lose readability on tiny inputs.
COMPRESSION_MIN_OUTPUT_CHARS: int = 2_000

# Performance ceiling: output larger than this skips compression and falls back
# to the existing head-truncation/persistence path.
COMPRESSION_MAX_INPUT_CHARS: int = 2_000_000


class ToolResultLimitConfig(BaseModel):
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


class EffectLedgerConfig(BaseModel):
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


class ToolSearchConfig(BaseModel):
    """Master switch for the Tool Search subsystem (deferred-tool discovery).

    Sibling of :class:`ToolResultLimitConfig` / :class:`EffectLedgerConfig`: a
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
