"""Native semantic output-compression layer (rtk port).

Tool output from git / pytest / ruff and friends floods the LLM context with
low-signal text. This package compresses that output *structurally* — it
understands each ecosystem's format and rewrites it into a compact summary,
preserving failures, counts, and locations while dropping the churn.

Public entry point: :func:`compress_output`. Everything is fail-safe — any
exception, an over-large input, or an unrecognised command all return the
original output unchanged, so compression can never break a tool call.
"""

from __future__ import annotations

from metagpt.executor.compress.base import (
    CompressionResult,
    Compressor,
    safe_compress,
    strip_ansi,
    unchanged,
)
from metagpt.executor.compress.registry import lookup_compressor
from metagpt.executor.permission.command_parse import command_prefix, prefix_tokens

__all__ = [
    "CompressionResult",
    "Compressor",
    "compress_output",
    "lookup_compressor",
    "safe_compress",
    "strip_ansi",
    "unchanged",
]


def compress_output(
    command: str,
    output: str,
    *,
    min_chars: int,
    max_input_chars: int,
) -> CompressionResult:
    """Compress a command's *output* when a compressor claims it.

    Returns an :func:`unchanged` result (the untouched, un-stripped output)
    when any of these hold:

    * *command* or *output* is empty;
    * *output* is shorter than *min_chars* (escape valve for small output);
    * *output* is longer than *max_input_chars* (performance guard — leave it
      to the downstream truncation layer);
    * no compressor claims the command's prefix; or
    * the compressor declines / would grow the output (grow-guard).

    On success the returned :attr:`CompressionResult.text` is derived from the
    ANSI-stripped output; on decline the *original* (possibly ANSI-bearing)
    output is preserved so no information is lost.
    """
    if not command or not output:
        return unchanged(output)

    n = len(output)
    if n < min_chars or n > max_input_chars:
        return unchanged(output)

    compressor = lookup_compressor(command_prefix(command), prefix_tokens(command))
    if compressor is None:
        return unchanged(output)

    result = safe_compress(compressor.compress, strip_ansi(output), prefix_tokens(command) or [])
    if result.applied:
        return result
    # Declined / grow-guarded: hand back the original output untouched.
    return unchanged(output)
