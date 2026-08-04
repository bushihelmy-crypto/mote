"""Settings config adapter — the standard ``settings.local.json`` permission file.

Permission rules can be declared per-project in ``.mote/settings.local.json``,
discovered by walking from the working directory up to the git root (plus a
user-level ``~/.mote/settings.local.json``) — the same layering the skills / MCP
subsystems use. The file shape follows the ``settings.local.json`` convention::

    {
      "permissions": {
        "allow": ["Read", "Search", "Bash(git*)"],
        "deny":  ["Bash(rm -rf*)"],
        "ask":   ["Edit", "Bash(npm publish*)"]
      }
    }

The rule strings are the familiar ``Tool`` / ``Tool(pattern)`` / ``mcp__server``
form already understood by :class:`~mote.runtime.tools.permission.config.PermissionConfig` and the
permission engine — so a block pasted from a ``.claude`` settings file works
verbatim.

Layering: ``~/.mote/settings.local.json`` (user) is the lowest layer, then each
``<dir>/.mote/settings.local.json`` from the git root down to *cwd* (closer wins).
The three lists are **unioned** across layers (order preserved, de-duplicated),
so a closer file only ever *adds* rules — it can't silently drop a farther
layer's ``deny`` (which would be a footgun for a bypass-immune rule).

Missing files contribute no overlay. Present malformed files fail closed before
the Product baseline is activated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Sequence

from mote.runtime.sandbox.config import SandboxProfile, SandboxRuntimeConfig
from mote.runtime.tools.permission.config import PermissionConfig, SandboxConfig

PRODUCT_PERMISSION_BASELINE_GENERATION = "mote.product-permissions/v1"

#: The canonical settings file name (a de-facto ecosystem convention). Lives under
#: each project's ``.mote/`` dir and under ``~/.mote/``. The ``.local`` marker
#: signals a git-ignored, machine-local overlay (see ``.gitignore``).
SETTINGS_FILE_NAME = "settings.local.json"

_RULE_KEYS = ("allow", "deny", "ask")


def settings_paths(paths: Sequence[Path] = ()) -> List[Path]:
    """All settings files to read, low→high precedence.

    ``~/.mote/settings.local.json`` (user) first, then every
    ``<dir>/.mote/settings.local.json`` found walking from *cwd* up to the git
    root (closer-to-cwd last, so it wins). Only existing files are returned; the
    list may be empty.
    """
    return list(paths)


def _extend_unique(dst: List[str], src: object) -> None:
    """Append each string in *src* to *dst*, skipping non-strings and dups."""
    if not isinstance(src, list):
        raise ValueError("permission rule collection must be a JSON list")
    seen = set(dst)
    for item in src:
        if not isinstance(item, str):
            raise ValueError("permission rule must be a string")
        spec = item.strip()
        if not spec:
            raise ValueError("permission rule must not be blank")
        if spec not in seen:
            dst.append(spec)
            seen.add(spec)


def load_permission_rules(
    paths: Sequence[Path] = (),
) -> Optional[PermissionConfig]:
    """Load allow/deny/ask rules from the ``.mote/settings.local.json`` walk.

    Files are read low→high (``~/.mote`` then the git-root→cwd walk); the three
    rule lists are unioned across layers (order preserved, de-duplicated). Returns
    a :class:`PermissionConfig` carrying only the ``allow`` / ``deny`` / ``ask``
    lists, or ``None`` when no file contributed a single rule (so a caller can
    leave a Role's existing permission policy untouched). Best-effort throughout:
    bad files / entries are dropped, never raised.
    """
    allow: List[str] = []
    deny: List[str] = []
    ask: List[str] = []
    buckets = {"allow": allow, "deny": deny, "ask": ask}

    for path in settings_paths(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"permission source is malformed: {path}") from exc
        if type(data) is not dict:
            raise ValueError(f"permission source root must be an object: {path}")
        perms = data.get("permissions")
        if perms is None:
            continue
        if type(perms) is not dict or set(perms) - set(_RULE_KEYS):
            raise ValueError(f"permission source has an invalid permissions section: {path}")
        for key in _RULE_KEYS:
            if key in perms:
                _extend_unique(buckets[key], perms[key])

    if not (allow or deny or ask):
        return None
    return PermissionConfig(allow=allow, deny=deny, ask=ask)


def build_product_permission_config(overlay: PermissionConfig | None) -> PermissionConfig:
    """Compile the immutable Product baseline plus a rules-only overlay."""
    rules = overlay or PermissionConfig()
    profile = SandboxProfile.NETWORKED_GOVERNED
    return PermissionConfig(
        mode=rules.mode,
        allow=list(rules.allow),
        deny=list(rules.deny),
        ask=list(rules.ask),
        sandbox=SandboxConfig(profile=profile),
        runtime=SandboxRuntimeConfig(profile=profile, network="proxy"),
    )


__all__ = [
    "PRODUCT_PERMISSION_BASELINE_GENERATION",
    "SETTINGS_FILE_NAME",
    "load_permission_rules",
    "build_product_permission_config",
    "settings_paths",
]
