#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""First-run scaffolding of the user-level ``~/.mote`` config home.

On CLI startup we want a fresh machine to come up with an editable set of config
templates rather than an empty (or missing) ``~/.mote`` directory. This module
owns that one-time seeding:

* ``~/.mote/config.yaml``       — copied verbatim from the shipped, fully
  annotated ``config.example.yaml`` (falls back to a minimal inline stub if the
  packaged example cannot be read).
* ``~/.mote/mcp.json``          — an empty ``{"mcpServers": {}}`` map, the shape
  every MCP client (Cursor/Cline/VS Code) uses; see
  :mod:`mote.product.config.adapters.mcp`.
* ``~/.mote/hooks.json``        — an empty ``{"hooks": {}}`` map of global
  agent-lifecycle hook rules that apply to every Role; see
  :mod:`mote.product.config.adapters.hooks`.
* ``~/.mote/secrets_config.json`` — an empty flat ``{}`` named-secret map read by
  :class:`mote.runtime.secrets.store.SecretStore`.
* ``~/.mote/skills/``           — the user-level skills directory scanned by the
  skills subsystem.

Two invariants make this safe to run on *every* launch:

* **Idempotent / non-destructive** — each item is created only when absent; an
  existing file is *never* overwritten, so a user's edited config is untouchable.
* **Best-effort** — any :class:`OSError` (read-only home, permission denied, …)
  is logged and swallowed. Scaffolding is a convenience, never a hard startup
  dependency: config loading already works purely off defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

from mote.runtime.telemetry.logging import logger

#: The MCP server file mote discovers (``executor.mcp.config_source``). An empty
#: map means "no servers configured"; a user pastes community blocks under it.
_MCP_TEMPLATE = {"mcpServers": {}}

#: The global hooks file discovered by the Product-owned hook config source. An empty
#: map means "no hooks configured"; a user adds event → matcher-group rules.
_HOOKS_TEMPLATE = {"hooks": {}}

#: The plaintext, human-edited named-secret file (a flat ``{name: value}`` map).
_SECRETS_CONFIG_TEMPLATE: dict = {}

#: The user-level skills directory scanned by the skills subsystem.
_SKILLS_DIR_NAME = "skills"

#: Minimal ``config.yaml`` used only if the packaged ``config.example.yaml``
#: cannot be located/read — enough for a user to fill in and run.
_CONFIG_FALLBACK = """\
# Mote user config. See config.example.yaml in the mote package for the full,
# annotated template. A canonical default route and its credential are required.
models:
  mode: shortcut
  default:
    api_key: "sk-your-api-key-here"  # pragma: allowlist secret
    api_type: anthropic
    base_url: "https://api.anthropic.com"
    model: "claude-opus-4-8"
"""


def _packaged_config_example(package_dir: Path) -> Path:
    """Path to the annotated ``config.example.yaml`` shipped inside the package."""
    return package_dir / "config.example.yaml"


def _config_yaml_template(package_dir: Path) -> str:
    """Return the ``config.yaml`` seed text (packaged example, else the stub)."""
    example = _packaged_config_example(package_dir)
    try:
        return example.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"bootstrap: could not read {example}, using minimal config template: {exc}")
        return _CONFIG_FALLBACK


def _seed_file(path: Path, content: str, *, label: str) -> None:
    """Write *content* to *path* only when it does not already exist (best-effort)."""
    if path.exists():
        return
    try:
        path.write_text(content, encoding="utf-8")
        logger.info(f"bootstrap: created {label} at {path}")
    except OSError as exc:
        logger.warning(f"bootstrap: could not create {label} at {path}: {exc}")


def ensure_mote_home(root: Path, *, package_dir: Path) -> None:
    """Scaffold the user-level ``~/.mote`` home with config templates if missing.

    Creates the config directory and seeds ``config.yaml`` / ``mcp.json`` /
    ``hooks.json`` / ``secrets_config.json`` / ``skills/`` — each only when
    absent. The destination and package data root are explicit composition
    inputs. Never raises: a hostile / read-only home is logged and ignored.
    """
    base = Path(root)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(f"bootstrap: could not create config home {base}: {exc}")
        return

    _seed_file(
        base / "config.yaml",
        _config_yaml_template(package_dir),
        label="config.yaml",
    )
    _seed_file(base / "mcp.json", json.dumps(_MCP_TEMPLATE, indent=2) + "\n", label="mcp.json")
    _seed_file(
        base / "hooks.json",
        json.dumps(_HOOKS_TEMPLATE, indent=2) + "\n",
        label="hooks.json",
    )
    _seed_file(
        base / "secrets_config.json",
        json.dumps(_SECRETS_CONFIG_TEMPLATE, indent=2) + "\n",
        label="secrets_config.json",
    )

    skills_dir = base / _SKILLS_DIR_NAME
    if not skills_dir.exists():
        try:
            skills_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"bootstrap: created skills dir at {skills_dir}")
        except OSError as exc:
            logger.warning(f"bootstrap: could not create skills dir {skills_dir}: {exc}")


__all__ = ["ensure_mote_home"]
