"""Render drained diagnostics into a context block for the model.

Turns ``{path: [Diagnostic, ...]}`` (the registry's drain output) into a compact
``<lsp_diagnostics>`` text block delivered as per-turn user context — mirroring
how memory_context / reminders are injected (ephemeral, not stored in history).

Lines look like::

    path/to/file.py
      Error [12:5] Undefined name 'foo' (pyflakes)
      Warning [3:1] Unused import 'os'

A file that was cleared shows ``(no diagnostics — resolved)`` so the model learns
a previously reported problem is fixed.
"""

from __future__ import annotations

from mote.runtime.agent.lsp.registry import Diagnostic, severity_label

_OPEN = "<lsp_diagnostics>"
_CLOSE = "</lsp_diagnostics>"


def format_diagnostics(changed: dict[str, list[Diagnostic]]) -> str:
    """Render the drained, changed diagnostics; "" when there's nothing to show."""
    if not changed:
        return ""

    lines: list[str] = [
        _OPEN,
        "Language-server diagnostics for files changed this session:",
    ]
    for path, diags in changed.items():
        lines.append(path)
        if not diags:
            lines.append("  (no diagnostics — resolved)")
            continue
        for d in diags:
            code = f" [{d.code}]" if d.code else ""
            src = f" ({d.source})" if d.source else ""
            # LSP positions are 0-based; surface them 1-based for the human/model.
            loc = f"{d.line + 1}:{d.character + 1}"
            lines.append(f"  {severity_label(d.severity)} [{loc}] {d.message}{code}{src}")
    lines.append(_CLOSE)
    return "\n".join(lines)


__all__ = ["format_diagnostics"]
