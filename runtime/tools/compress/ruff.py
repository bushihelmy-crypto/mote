"""Compressor for ``ruff`` / ``flake8`` (and ``ruff check``) output.

A lint run over a large tree emits one line per finding
(``path:line:col: CODE message``). The high-signal summary is: which rule
codes fired, how many times each, a few example locations, and the trailing
``Found K errors`` count. Findings are grouped by code; the first few
locations of each are kept.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from mote.contracts.text import count_noun
from mote.runtime.tools.compress.base import CompressionResult, applied, unchanged

# ``path:line:col: CODE message`` — ruff and flake8 share this shape.
_FINDING_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>[A-Z]+\d+)\s+(?P<msg>.*)$")

# Locations to show per rule code before summarising the rest.
_KEEP_PER_CODE = 3


class RuffCompressor:
    """Group findings by rule code with a count + a few example locations."""

    prefixes: tuple[str, ...] = ("ruff", "ruff check", "flake8")

    def compress(self, output: str, *, argv: list[str]) -> CompressionResult:
        groups: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict()
        footer: list[str] = []

        for line in output.splitlines():
            m = _FINDING_RE.match(line)
            if m:
                loc = f"{m.group('path')}:{m.group('line')}:{m.group('col')}"
                groups.setdefault(m.group("code"), []).append((loc, m.group("msg")))
                continue
            stripped = line.strip()
            if stripped.startswith("Found ") or "fixable" in stripped or stripped.startswith("[*]"):
                footer.append(line)
            # everything else (blank lines, unparsed noise): dropped

        if not groups:
            # Nothing recognised — don't touch it (let the truncation layer act).
            return unchanged(output, label="ruff")

        out: list[str] = []
        for code, occ in groups.items():
            out.append(f"{code}: {count_noun(len(occ), 'occurrence')}")
            for loc, msg in occ[:_KEEP_PER_CODE]:
                out.append(f"  {loc}: {msg}")
            if len(occ) > _KEEP_PER_CODE:
                out.append(f"  ... and {len(occ) - _KEEP_PER_CODE} more")
        out.extend(footer)

        return applied(output, "\n".join(out), "ruff")
