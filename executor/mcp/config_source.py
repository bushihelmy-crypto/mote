"""MCP server config source — the Claude-style ``mcpServers`` JSON file.

MCP server definitions live in their *own* file (``mcp_config.json`` under the
package source root), NOT in the layered ``config.yaml``. This is deliberate:

* **Ecosystem standard.** Claude Desktop / Cursor / Cline / VS Code all use the
  same ``{"mcpServers": {name: {...}}}`` shape, so a user can paste a community
  server block verbatim — zero translation.
* **Hot-reload seam.** A single, well-known file is the natural thing for the
  file watcher to observe; a change re-inits MCP without touching the rest of
  the config (see ``executor.reload_mcp``).
* **Map kills a validator.** The server name is the map key, so uniqueness is
  structural — the old ``MCPConfig.validate_unique_server_names`` is unneeded.

The transport ``type`` is *inferred*, never declared: a ``command`` means STDIO,
a ``url`` means SSE. Presence in the map means enabled; delete an entry to
disable it. These conventions match the ecosystem files and remove every
redundant field.

Everything is best-effort: a missing / empty / malformed file yields an empty
server list (MCP simply stays unconfigured), never an exception into wiring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from metagpt.common.config.config.mcp_config import MCPServerConfig, MCPTransportType
from metagpt.common.const import SOURCE_ROOT
from metagpt.common.logs import logger

#: The canonical MCP config file name (Claude-ecosystem convention lives under
#: the package source root so it sits beside ``config.yaml``).
MCP_CONFIG_FILE_NAME = "mcp_config.json"


def mcp_config_path(cwd: Optional[Path] = None) -> Path:
    """The on-disk path of the MCP server config file.

    Anchored at :data:`SOURCE_ROOT` (the package root's ``metagpt/`` dir) so it
    resolves the same regardless of the process cwd — the file lives beside the
    project ``config.yaml``. ``cwd`` is accepted for symmetry with the config
    loaders and future per-workspace overrides; unused today.
    """
    return SOURCE_ROOT / MCP_CONFIG_FILE_NAME


def _to_server_config(name: str, spec: dict) -> Optional[MCPServerConfig]:
    """Adapt one Claude-style server entry into an :class:`MCPServerConfig`.

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
    )


def load_mcp_servers(cwd: Optional[Path] = None) -> list[MCPServerConfig]:
    """Load all configured MCP servers from ``mcp_config.json``.

    Reads the Claude-style ``{"mcpServers": {name: {...}}}`` map and adapts each
    entry to an :class:`MCPServerConfig`. Best-effort: a missing / empty /
    malformed file (or a bad individual entry) yields an empty list / drops that
    entry rather than raising, so MCP just stays unconfigured.
    """
    path = mcp_config_path(cwd)
    if not path.is_file():
        return []

    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(f"MCP config: could not read {path}: {exc}")
        return []
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(f"MCP config: {path} is not valid JSON: {exc}")
        return []

    servers_map = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers_map, dict):
        logger.warning(f"MCP config: {path} has no 'mcpServers' object.")
        return []

    servers: list[MCPServerConfig] = []
    for name, spec in servers_map.items():
        cfg = _to_server_config(name, spec)
        if cfg is not None:
            servers.append(cfg)
    return servers


__all__ = ["MCP_CONFIG_FILE_NAME", "mcp_config_path", "load_mcp_servers"]
