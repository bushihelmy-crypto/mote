"""Stub compressor for ``cargo`` output.

Placeholder so the ecosystem is registered but currently a no-op. A future
implementation would fold ``cargo build`` / ``cargo test`` output the way
:mod:`mote.runtime.tools.compress.pytest` does: keep ``error[EXXXX]`` / warning
blocks and the final ``error: could not compile`` / test summary line, drop the
``Compiling <crate>`` progress churn. Until then it declines every input.
"""

from __future__ import annotations

from mote.runtime.tools.compress.base import CompressionResult, unchanged


class CargoCompressor:
    """No-op stub — declines all input (``applied=False``)."""

    prefixes: tuple[str, ...] = ("cargo",)

    def compress(self, output: str, *, argv: list[str]) -> CompressionResult:
        return unchanged(output, label="cargo")
