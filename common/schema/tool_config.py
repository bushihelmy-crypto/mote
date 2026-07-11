"""Tool execution config and constants — consolidated from mote.executor/tool_result_limit.py.

The enforcement logic stays in ``mote.executor.tool_result_limit``; only the
pure-data config model and constants live here so any layer can reference them
without importing the executor package.
"""

from __future__ import annotations

from pydantic import BaseModel

from mote.common.text import PERSISTED_OUTPUT_CLOSE, PERSISTED_OUTPUT_OPEN

# ---------------------------------------------------------------------------
# Tool-level constants (CC ``constants/toolLimits.ts``)
# ---------------------------------------------------------------------------

# System-wide default cap on a single tool result, in characters. A tool may
# declare a lower (or higher) value via ``BaseTool.max_result_size_chars``.
DEFAULT_MAX_RESULT_SIZE_CHARS: int = 50_000

# Conservative bytes-per-token estimate (CC ``BYTES_PER_TOKEN``).
BYTES_PER_TOKEN: int = 4

# When a tool result is persisted to disk, this many leading bytes are kept
# inline as a preview (CC ``PREVIEW_SIZE_BYTES``).
PREVIEW_SIZE_BYTES: int = 2_000

# XML-ish envelope wrapping a persisted tool result's inline preview. The literal
# is owned by the marker authority (``common/text/markers.py``); aliased here under
# the historical ``*_TAG`` names the executor already imports.
PERSISTED_OUTPUT_OPEN_TAG: str = PERSISTED_OUTPUT_OPEN
PERSISTED_OUTPUT_CLOSE_TAG: str = PERSISTED_OUTPUT_CLOSE

# Per-tool default caps, aligned with CC's per-tool ``maxResultSizeChars``.
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

# Subdirectory (under the workspace root) that holds persisted tool results.
TOOL_RESULTS_SUBDIR = ".tool_results"

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
