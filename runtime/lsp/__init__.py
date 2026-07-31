"""LSP subsystem — opt-in language-server diagnostics for the Role.

A service-only MVP (no interactive LSP tool): when a Role is configured with an
``LspConfig`` (servers + extensions), :class:`LspService` handles telemetry
``FileMutatedEvent`` observations (a tool write) by lazily
launching the matching language server, syncing the document over JSON-RPC, and
broadcasting any *changed* diagnostics as a ``DiagnosticsEvent``. The
:class:`DiagnosticsBuffer` (also a telemetry handler) accumulates those blocks; at
the next turn boundary the Role drains the buffer into per-turn context.

Layering: lives in ``roles`` (the top layer) because it's owned by the Role and
torn down with the session. The executor never names this package — it merely
emits a ``FileMutatedEvent`` that this service receives from Telemetry, and
diagnostics flow back out on Telemetry as a ``DiagnosticsEvent``.
"""

from mote.runtime.lsp.buffer import DiagnosticsBuffer
from mote.runtime.lsp.service import LspService

__all__ = ["LspService", "DiagnosticsBuffer"]
