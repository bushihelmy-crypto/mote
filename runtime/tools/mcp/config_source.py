"""MCP server config source — the standard ``mcpServers`` JSON file.

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

Everything is best-effort: a missing / empty / malformed file yields an empty
server list (MCP simply stays unconfigured), never an exception into wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from mote.contracts.config.mcp import MCPServerConfig, MCPTransportType
from mote.contracts.config.oauth import OAuthProviderConfig
from mote.runtime.logging import logger
from mote.runtime.paths import load_mote_json_section, mote_layered_files

#: The canonical MCP config file name (the de-facto MCP convention). Lives under
#: each project's ``.mote/`` dir and under ``~/.mote/``.
MCP_CONFIG_FILE_NAME = "mcp.json"


def mcp_config_paths(cwd: Optional[Path] = None) -> List[Path]:
    """All MCP config files to read, low→high precedence.

    ``~/.mote/mcp.json`` (user) first, then every ``<dir>/.mote/mcp.json`` found
    walking from *cwd* up to the git root (closer-to-cwd last, so it wins). Only
    existing files are returned; the list may be empty.
    """
    return mote_layered_files(MCP_CONFIG_FILE_NAME, cwd)


def _to_server_config(name: str, spec: dict) -> Optional[MCPServerConfig]:
    """Adapt one standard server entry into an :class:`MCPServerConfig`.

    Transport is inferred from the shape: a ``url`` => SSE, else a ``command``
    => STDIO. An entry with neither is malformed and dropped (logged). Presence
    in the map means enabled, so ``enabled=True`` is stamped unconditionally.
    """
    if not isinstance(spec, dict):
        logger.warning(f"MCP config: server '{name}' is not an object, skipping.")
        return None

    url = spec.get("url")
    command = spec.get("command")
    if url:
        transport = MCPTransportType.SSE
    elif command:
        transport = MCPTransportType.STDIO
    else:
        logger.warning(f"MCP config: server '{name}' has neither 'url' nor 'command', skipping.")
        return None

    return MCPServerConfig(
        name=name,
        type=transport,
        enabled=True,
        url=url,
        command=command,
        args=list(spec.get("args") or []),
        env=dict(spec.get("env") or {}),
        aliases=dict(spec.get("aliases") or {}),
        oauth=_to_oauth_config(name, spec.get("oauth")),
    )


def _to_oauth_config(name: str, raw: object) -> Optional[OAuthProviderConfig]:
    """Adapt an optional ``oauth`` block into an :class:`OAuthProviderConfig`.

    Absent => ``None`` (the server stays unauthenticated). A malformed block is
    dropped (logged) rather than raised, so one bad ``oauth`` entry never sinks
    the whole server — it just loads without auth, matching the loader's
    best-effort contract.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(f"MCP config: server '{name}' has a non-object 'oauth' block, ignoring it.")
        return None
    try:
        return OAuthProviderConfig(**raw)
    except Exception as e:
        logger.warning(f"MCP config: server '{name}' has an invalid 'oauth' block ({e}); loading without auth.")
        return None


def load_mcp_servers(cwd: Optional[Path] = None) -> List[MCPServerConfig]:
    """Load all configured MCP servers, merged across the ``.mote/mcp.json`` walk.

    Files are read low→high (``~/.mote/mcp.json`` then the git-root→cwd walk);
    a later (closer-to-cwd) file's server of the same name overrides an earlier
    one. Each entry is adapted from the standard ``{"mcpServers": {...}}``
    map. Best-effort throughout: bad files / entries are dropped, never raised.
    """
    merged: dict[str, dict] = {}
    for path in mcp_config_paths(cwd):
        section = load_mote_json_section(path, "mcpServers", "MCP config")
        for name, spec in section.items():
            merged[name] = spec  # closer file overrides farther (walk is low→high)

    servers: List[MCPServerConfig] = []
    for name, spec in merged.items():
        cfg = _to_server_config(name, spec)
        if cfg is not None:
            servers.append(cfg)
    return servers


__all__ = ["MCP_CONFIG_FILE_NAME", "mcp_config_paths", "load_mcp_servers"]
