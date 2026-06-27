"""Findings + report formatting for the code-review pipeline.

Defines the :class:`Finding` produced per comment and renders a batch of
findings as either human-readable text (grouped by file) or machine-readable
JSON.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import List, Optional


@dataclass
class Finding:
    """A single review comment, located onto concrete new-side line numbers.

    ``start_line`` / ``end_line`` are ``None`` when the resolver could not map
    the agent's ``existing_code`` snippet back onto the diff.
    """

    file: str
    severity: str = "info"
    message: str = ""
    existing_code: str = ""
    start_line: Optional[int] = None
    end_line: Optional[int] = None


def _line_label(f: Finding) -> str:
    """Render the location label for a finding (``L12`` / ``L12-15`` / ``L?``)."""
    if f.start_line is None:
        return "L?"
    if f.end_line is None or f.end_line == f.start_line:
        return f"L{f.start_line}"
    return f"L{f.start_line}-{f.end_line}"


def format_findings(findings: List[Finding], fmt: str = "text") -> str:
    """Render *findings* as ``text`` (human) or ``json`` (machine).

    Args:
        findings: The findings to render.
        fmt: ``"text"`` (default) groups by file with ``path:line [severity] msg``
            lines; ``"json"`` emits a JSON array of finding objects.
    """
    if fmt == "json":
        return json.dumps([asdict(f) for f in findings], ensure_ascii=False, indent=2)

    if not findings:
        return "Code review complete — no issues found."

    # Group by file, preserving first-seen order.
    grouped: dict[str, List[Finding]] = {}
    for f in findings:
        grouped.setdefault(f.file, []).append(f)

    lines: List[str] = []
    total = len(findings)
    lines.append(f"Code review found {total} issue(s) across {len(grouped)} file(s):")
    lines.append("")
    for path, items in grouped.items():
        lines.append(f"## {path}")
        for f in items:
            label = _line_label(f)
            lines.append(f"  {path}:{label}  [{f.severity}] {f.message}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
