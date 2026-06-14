"""LSP subsystem — opt-in language-server diagnostics for the Role.

A service-only MVP (no interactive LSP tool): when a Role is configured with an
``LspConfig`` (servers + extensions), file-mutating tools poke :class:`LspService`
after each write; it lazily launches the matching language server, syncs the
document over JSON-RPC, and stages published diagnostics. At the next turn
boundary the Role drains any *changed* diagnostics into per-turn context.

Layering: lives in ``roles`` (the top layer) because it's owned by the Role and
torn down with the session. The executor depends only on the
:class:`metagpt.common.interface.LspNotifier` Protocol, never on this package.
"""

from metagpt.roles.lsp.service import LspService

__all__ = ["LspService"]
