"""LSP subsystem — opt-in language-server diagnostics for the Role.

A service-only capability (no interactive LSP tool): when a Role is configured
with an ``LspConfig``, :class:`LspService` reliably projects committed File
Operations versions by lazily launching the matching language server, syncing
the document over JSON-RPC, and broadcasting changed diagnostics. The
:class:`DiagnosticsBuffer` (also a telemetry handler) accumulates those blocks; at
the next turn boundary the Role drains the buffer into per-turn context.

The Role session owns its lifecycle and EventFabric checkpoint. Diagnostics
flow back out on Telemetry as advisory ``DiagnosticsEvent`` values.
"""

from mote.runtime.lsp.buffer import DiagnosticsBuffer
from mote.runtime.lsp.service import LspService

__all__ = ["LspService", "DiagnosticsBuffer"]
