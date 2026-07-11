"""Stub compressor for ``pip`` output.

Placeholder so the ecosystem is registered but currently a no-op. A future
implementation would collapse ``pip install`` progress: keep the
``Successfully installed ...`` / ``ERROR: ...`` result lines, drop the
per-package ``Downloading`` / ``Collecting`` / progress-bar churn. Until then
it declines every input.
"""

from __future__ import annotations

from mote.executor.compress.base import CompressionResult, unchanged


class PipCompressor:
    """No-op stub — declines all input (``applied=False``)."""

    prefixes: tuple[str, ...] = ("pip", "pip3")

    def compress(self, output: str, *, argv: list[str]) -> CompressionResult:
        return unchanged(output, label="pip")
