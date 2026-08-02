"""MCP server config adapter — the standard ``mcpServers`` JSON file.

MCP server definitions live in their *own* file, ``.mote/mcp.json``, discovered
per-project by walking from the working directory up to the git root (plus a
user-level ``~/.mote/mcp.json``) — NOT in the layered ``config.yaml``. This is
deliberate:

* **Ecosystem standard.** Cursor / Cline / VS Code and other MCP clients all use the
  same ``{"mcpServers": {name: {...}}}`` shape, so a user can paste a community
  server block verbatim — zero translation.
* **Per-project + user layering.** The ``<dir>/.mote/mcp.json`` walk mirrors the
  skills subsystem: a closer
  file overrides a farther one, and ``~/.mote/mcp.json`` is the lowest layer.
* **Hot-reload seam.** A single, well-known file name is the natural thing for
  the file watcher to observe; a change re-inits MCP without touching the rest
  of the config (see ``executor.reload_mcp``).
* **Map makes a validator unnecessary.** The server name is the map key, so
  uniqueness is structural — no explicit uniqueness validator is needed.

The transport ``type`` is *inferred*, never declared: a ``command`` means STDIO,
a ``url`` means SSE. Presence in the map means enabled; delete an entry to
disable it. These conventions match the ecosystem files and remove every
redundant field.

Approved sources compile as one candidate: any malformed server or declared
authentication block rejects the candidate before Runtime activation.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.contracts.tool.transport import MCPTransportType
from mote.product.config.layered_json import decode_json_section
from mote.product.extensions.sources import ExtensionKind, ExtensionSource
from mote.runtime.config.mcp import MCPServerConfig

#: The canonical MCP config file name (the de-facto MCP convention). Lives under
#: each project's ``.mote/`` dir and under ``~/.mote/``.
MCP_CONFIG_FILE_NAME = "mcp.json"


class McpConfigCompilationError(ValueError):
    """A source-located failure that prevents candidate MCP activation."""

    def __init__(self, source: Path, server_name: str, detail: str) -> None:
        self.source = source
        self.server_name = server_name
        self.detail = detail
        super().__init__(f"MCP candidate rejected at {source} server {server_name!r}: {detail}")


def mcp_config_paths(sources: Sequence[ExtensionSource] = ()) -> List[ExtensionSource]:
    """All MCP config files to read, low→high precedence.

    ``~/.mote/mcp.json`` (user) first, then every ``<dir>/.mote/mcp.json`` found
    walking from *cwd* up to the git root (closer-to-cwd last, so it wins). Only
    existing files are returned; the list may be empty.
    """
    return list(sources)


def _to_server_config(name: str, spec: object, source: Path) -> MCPServerConfig:
    """Adapt one standard server entry into an :class:`MCPServerConfig`.

    Transport is inferred from the shape: a ``url`` => SSE, else a ``command``
    => STDIO. An entry with neither is malformed and dropped (logged). Presence
    in the map means enabled, so ``enabled=True`` is stamped unconditionally.
    """
    if not isinstance(spec, dict):
        raise McpConfigCompilationError(source, name, "server declaration must be an object")

    url = spec.get("url")
    command = spec.get("command")
    if url:
        transport = MCPTransportType.SSE
    elif command:
        transport = MCPTransportType.STDIO
    else:
        raise McpConfigCompilationError(source, name, "server requires exactly one transport target")

    if bool(url) == bool(command):
        raise McpConfigCompilationError(source, name, "server must declare exactly one of url or command")

    try:
        return MCPServerConfig(
            name=name,
            type=transport,
            enabled=True,
            url=url,
            command=command,
            args=list(spec.get("args") or []),
            env=dict(spec.get("env") or {}),
            aliases=dict(spec.get("aliases") or {}),
            oauth=(_to_oauth_config(name, spec["oauth"], source) if "oauth" in spec else None),
        )
    except McpConfigCompilationError:
        raise
    except (TypeError, ValueError) as error:
        raise McpConfigCompilationError(source, name, f"invalid server declaration: {error}") from error


def _to_oauth_config(name: str, raw: object, source: Path) -> OAuthProviderConfig:
    """Adapt an optional ``oauth`` block into an :class:`OAuthProviderConfig`.

    This is called only when the key was declared. Absence is handled by the
    caller; null or malformed declarations fail closed.
    """
    if not isinstance(raw, dict):
        raise McpConfigCompilationError(source, name, "declared oauth must be an object")
    try:
        return OAuthProviderConfig(**raw)
    except (TypeError, ValueError) as error:
        raise McpConfigCompilationError(source, name, f"invalid oauth declaration: {error}") from error


def load_mcp_servers(
    sources: Sequence[ExtensionSource] = (),
) -> List[MCPServerConfig]:
    """Load all configured MCP servers, merged across the ``.mote/mcp.json`` walk.

    Files are read low→high (``~/.mote/mcp.json`` then the git-root→cwd walk);
    a later (closer-to-cwd) file's server of the same name overrides an earlier
    one. Each entry is adapted from the standard ``{"mcpServers": {...}}``
    map. Best-effort throughout: bad files / entries are dropped, never raised.
    """
    merged: dict[str, tuple[object, Path]] = {}
    for source in mcp_config_paths(sources):
        if source.kind is not ExtensionKind.MCP or not source.approved:
            raise ValueError("MCP config requires an approved MCP source")
        section = decode_json_section(source.content, source.canonical_path, "mcpServers")
        for name, spec in section.items():
            if not isinstance(name, str) or not name:
                raise McpConfigCompilationError(source.canonical_path, str(name), "server name must be non-empty")
            merged[name] = (spec, source.canonical_path)

    servers: List[MCPServerConfig] = []
    for name, (spec, source) in merged.items():
        servers.append(_to_server_config(name, spec, source))
    return servers


__all__ = [
    "MCP_CONFIG_FILE_NAME",
    "McpConfigCompilationError",
    "mcp_config_paths",
    "load_mcp_servers",
]
