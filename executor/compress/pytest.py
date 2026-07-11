"""Compressor for ``pytest`` / ``py.test`` output.

pytest output is dominated by noise for the model's purposes: the per-file
progress dots (``tests/foo.py ....F.. [ 50%]``) and PASSED lines in verbose
mode. What matters is kept verbatim — the ``FAILURES`` / ``ERRORS`` sections
(with their tracebacks), the ``short test summary info`` lines, and the final
``N failed, M passed`` result line. A line-oriented state machine keeps the
signal and drops the progress churn.
"""

from __future__ import annotations

import re

from mote.executor.compress.base import CompressionResult, applied

# A pytest section banner: a line of ``=`` with a section title in it.
_SECTION_RE = re.compile(r"^=+.*=+\s*$")
# A progress line ends with a ``[ NN%]`` marker (both terse and verbose modes).
_PROGRESS_RE = re.compile(r"\[\s*\d+%\]\s*$")


def _section_name(line: str) -> str:
    """Return the lower-cased title of a banner line (``""`` if not a banner)."""
    if not _SECTION_RE.match(line):
        return ""
    return line.strip("= ").strip().lower()


class PytestCompressor:
    """Keep failures/errors/summary verbatim; drop progress + PASSED noise."""

    prefixes: tuple[str, ...] = ("pytest", "py.test")

    def compress(self, output: str, *, argv: list[str]) -> CompressionResult:
        out: list[str] = []
        # ``verbatim`` regions are copied line-for-line; ``drop`` regions are
        # skipped entirely (warnings summary body). Outside any region we are in
        # the header/progress zone: keep everything except progress churn.
        verbatim = False
        drop = False

        for line in output.splitlines():
            name = _section_name(line)
            if name:
                if name.startswith("failures") or name.startswith("errors"):
                    verbatim, drop = True, False
                    out.append(line)
                    continue
                if name.startswith("short test summary"):
                    verbatim, drop = True, False
                    out.append(line)
                    continue
                if name.startswith("warnings summary"):
                    verbatim, drop = False, True  # drop the warnings body
                    continue
                # Any other banner (session starts, final result line, ...):
                # ends a drop region and is always kept.
                verbatim, drop = False, False
                out.append(line)
                continue

            if drop:
                continue
            if verbatim:
                out.append(line)
                continue

            # Header / progress zone.
            if "FAILED" in line or "ERROR" in line:
                out.append(line)  # keep explicit failures even amid progress
            elif _PROGRESS_RE.search(line):
                continue  # drop ``[ NN%]`` progress + verbose PASSED lines
            else:
                out.append(line)

        return applied(output, "\n".join(out), "pytest")
