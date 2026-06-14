"""Diagnostic registry — dedup + volume-limit published diagnostics.

Language servers re-publish the *full* diagnostic set for a file on every
``publishDiagnostics`` (the latest set replaces the previous one). This registry
holds the current set per file (last-write-wins), and when the service drains it
for delivery into context it:

- caps per-file diagnostics (``_MAX_PER_FILE``) and total volume
  (``_MAX_TOTAL``) so a noisy server can't flood the model's context;
- only surfaces files whose diagnostics changed since the last drain, so the
  model isn't re-shown identical errors turn after turn.

Modeled on Claude Code's ``LSPDiagnosticRegistry`` (LRU dedup + volume limit).
Pure data + bookkeeping; no I/O, no LSP knowledge beyond the diagnostic shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Volume caps, mirroring Claude Code's defaults.
_MAX_PER_FILE = 10
_MAX_TOTAL = 30


@dataclass
class Diagnostic:
    """One diagnostic for a file (subset of the LSP Diagnostic shape)."""

    severity: int  # 1=Error, 2=Warning, 3=Information, 4=Hint
    line: int  # 0-based, as LSP reports it
    character: int
    message: str
    source: str = ""
    code: str = ""

    def key(self) -> tuple:
        """Identity for dedup/change-detection (ignores nothing meaningful)."""
        return (self.severity, self.line, self.character, self.message, self.source, self.code)


@dataclass
class DiagnosticRegistry:
    """Current diagnostics per file + change tracking for delivery.

    ``_by_file`` is the live set (replaced wholesale on each publish).
    ``_delivered`` remembers the key-set last drained per file so unchanged sets
    aren't re-surfaced.
    """

    _by_file: dict[str, list[Diagnostic]] = field(default_factory=dict)
    _delivered: dict[str, frozenset] = field(default_factory=dict)

    def publish(self, path: str, diagnostics: list[Diagnostic]) -> None:
        """Replace the diagnostic set for *path* (last-write-wins).

        An empty list clears the file (and is itself a change if it previously
        had diagnostics — lets the model see that errors were fixed).
        """
        if diagnostics:
            self._by_file[path] = list(diagnostics)
        else:
            self._by_file.pop(path, None)

    def has_changes(self) -> bool:
        """True if any file's current set differs from what was last drained."""
        for path, diags in self._by_file.items():
            if self._keyset(diags) != self._delivered.get(path, frozenset()):
                return True
        # A file that was cleared (now absent) but previously delivered is a change.
        for path in self._delivered:
            if path not in self._by_file and self._delivered[path]:
                return True
        return False

    def drain_changed(self) -> dict[str, list[Diagnostic]]:
        """Return changed files' (capped) diagnostics and mark them delivered.

        Only files whose key-set changed since the last drain are included.
        Per-file and total volume caps are applied (errors prioritised over
        warnings/hints). Files cleared since last delivery appear with an empty
        list so the caller can report "resolved".
        """
        changed: dict[str, list[Diagnostic]] = {}
        total = 0

        for path, diags in self._by_file.items():
            keyset = self._keyset(diags)
            if keyset == self._delivered.get(path, frozenset()):
                continue
            capped = self._cap_file(diags)
            if total + len(capped) > _MAX_TOTAL:
                capped = capped[: max(0, _MAX_TOTAL - total)]
            changed[path] = capped
            total += len(capped)
            self._delivered[path] = keyset
            if total >= _MAX_TOTAL:
                break

        # Report files cleared since last delivery.
        for path in list(self._delivered):
            if path not in self._by_file and self._delivered[path]:
                changed.setdefault(path, [])
                self._delivered[path] = frozenset()

        return changed

    @staticmethod
    def _cap_file(diags: list[Diagnostic]) -> list[Diagnostic]:
        """Cap to ``_MAX_PER_FILE``, prioritising more-severe diagnostics."""
        ordered = sorted(diags, key=lambda d: (d.severity, d.line, d.character))
        return ordered[:_MAX_PER_FILE]

    @staticmethod
    def _keyset(diags: list[Diagnostic]) -> frozenset:
        return frozenset(d.key() for d in diags)


def severity_label(severity: int) -> str:
    """Human label for an LSP severity code."""
    return {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}.get(severity, "Diag")


def parse_diagnostic(raw: dict) -> Optional[Diagnostic]:
    """Build a :class:`Diagnostic` from an LSP diagnostic object, or None.

    Tolerates missing/odd fields (best-effort): a diagnostic without a usable
    range/message is skipped rather than raising.
    """
    if not isinstance(raw, dict):
        return None
    rng = raw.get("range") or {}
    start = rng.get("start") or {}
    message = raw.get("message")
    if not isinstance(message, str) or not message:
        return None
    code = raw.get("code", "")
    return Diagnostic(
        severity=int(raw.get("severity", 1) or 1),
        line=int(start.get("line", 0) or 0),
        character=int(start.get("character", 0) or 0),
        message=message,
        source=str(raw.get("source", "") or ""),
        code=str(code) if code is not None else "",
    )


__all__ = ["Diagnostic", "DiagnosticRegistry", "parse_diagnostic", "severity_label"]
