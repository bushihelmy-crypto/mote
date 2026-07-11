"""LSP subsystem — opt-in language-server diagnostics for the Role.

A service-only MVP (no interactive LSP tool): when a Role is configured with an
``LspConfig`` (servers + extensions), :class:`LspService` subscribes to the
agent event bus and reacts to each ``FileMutatedEvent`` (a tool write) by lazily
launching the matching language server, syncing the document over JSON-RPC, and
broadcasting any *changed* diagnostics as a ``DiagnosticsEvent``. The
:class:`DiagnosticsBuffer` (also a bus subscriber) accumulates those blocks; at
the next turn boundary the Role drains the buffer into per-turn context.

Layering: lives in ``roles`` (the top layer) because it's owned by the Role and
torn down with the session. The executor never names this package — it merely
emits a ``FileMutatedEvent`` that this service subscribes to off the shared bus,
and diagnostics flow back out onto the same bus as a ``DiagnosticsEvent``.
"""

from mote.roles.lsp.buffer import DiagnosticsBuffer
from mote.roles.lsp.service import LspService

__all__ = ["LspService", "DiagnosticsBuffer"]
