"""Core types + fail-safe scaffolding for the output-compression layer.

A *compressor* understands the structure of one tool ecosystem's output
(git, pytest, ruff, ...) and rewrites verbose output into a compact,
token-cheap summary while preserving the signal that matters (failures,
counts, locations). Everything here is a leaf: this module imports only the
Python stdlib, so the ``executor.compress`` package never reaches up into the
executor's heavier dependencies.

The two guarantees callers rely on:

* **fail-safe** — :func:`safe_compress` swallows any exception raised by a
  compressor and returns the output unchanged. A broken compressor can never
  break a tool call.
* **grow-guard** — a "compression" that ends up *larger* than the input is
  treated as not-applied, so we never make output worse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


@dataclass
class CompressionResult:
    """Outcome of running (or declining to run) a compressor on some output.

    Attributes:
        text: The text to hand back to the caller. When ``applied`` is False
            this is the original output, untouched.
        applied: Whether compression actually changed the output.
        original_chars: Length of the input the compressor saw.
        compressed_chars: Length of :attr:`text`.
        label: Short tag naming the compressor that ran (e.g. ``"git log"``),
            for the marker line and debug logging.
    """

    text: str
    applied: bool
    original_chars: int
    compressed_chars: int
    label: str = ""

    @property
    def saved_chars(self) -> int:
        """Characters removed by compression (0 when not applied)."""
        return self.original_chars - self.compressed_chars if self.applied else 0


@runtime_checkable
class Compressor(Protocol):
    """A structure-aware compressor for one command family."""

    prefixes: tuple[str, ...]

    def compress(self, output: str, *, argv: list[str]) -> CompressionResult:
        """Compress *output*; return :func:`unchanged` when it declines."""
        ...


def unchanged(output: str, label: str = "") -> CompressionResult:
    """A not-applied result carrying *output* verbatim."""
    n = len(output)
    return CompressionResult(text=output, applied=False, original_chars=n, compressed_chars=n, label=label)


def applied(original: str, text: str, label: str) -> CompressionResult:
    """An applied result derived from *original* -> *text*."""
    return CompressionResult(
        text=text,
        applied=True,
        original_chars=len(original),
        compressed_chars=len(text),
        label=label,
    )


def safe_compress(
    fn: Callable[..., CompressionResult],
    output: str,
    argv: list[str],
) -> CompressionResult:
    """Run *fn* under fail-safe + grow-guard.

    Any exception, a non-:class:`CompressionResult` return, or a result that
    is no smaller than the input all collapse to :func:`unchanged` — the
    compressor is never allowed to lose or bloat a tool's output.
    """
    try:
        result = fn(output, argv=argv)
    except Exception:  # noqa: BLE001 — compression must never break a tool call
        return unchanged(output)
    if not isinstance(result, CompressionResult):
        return unchanged(output)
    if result.applied and result.compressed_chars >= result.original_chars:
        # Grow-guard: pretend it never ran rather than return larger output.
        return unchanged(output, label=result.label)
    return result
