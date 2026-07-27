"""Settings config source — the standard ``settings.local.json`` permission file.

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
form already understood by :class:`~mote.contracts.schema.PermissionConfig` and the
permission engine — so a block pasted from a ``.claude`` settings file works
verbatim.

Layering: ``~/.mote/settings.local.json`` (user) is the lowest layer, then each
``<dir>/.mote/settings.local.json`` from the git root down to *cwd* (closer wins).
The three lists are **unioned** across layers (order preserved, de-duplicated),
so a closer file only ever *adds* rules — it can't silently drop a farther
layer's ``deny`` (which would be a footgun for a bypass-immune rule).

Everything is best-effort: a missing / empty / malformed file contributes no
rules (permissions simply stay unconfigured), never an exception into wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from mote.contracts.settings.permissions import PermissionConfig
from mote.runtime.paths import load_mote_json_section, mote_layered_files

#: The canonical settings file name (a de-facto ecosystem convention). Lives under
#: each project's ``.mote/`` dir and under ``~/.mote/``. The ``.local`` marker
#: signals a git-ignored, machine-local overlay (see ``.gitignore``).
SETTINGS_FILE_NAME = "settings.local.json"

_RULE_KEYS = ("allow", "deny", "ask")


def settings_paths(cwd: Optional[Path] = None) -> List[Path]:
    """All settings files to read, low→high precedence.

    ``~/.mote/settings.local.json`` (user) first, then every
    ``<dir>/.mote/settings.local.json`` found walking from *cwd* up to the git
    root (closer-to-cwd last, so it wins). Only existing files are returned; the
    list may be empty.
    """
    return mote_layered_files(SETTINGS_FILE_NAME, cwd)


def _extend_unique(dst: List[str], src) -> None:
    """Append each string in *src* to *dst*, skipping non-strings and dups."""
    if not isinstance(src, (list, tuple)):
        return
    seen = set(dst)
    for item in src:
        if not isinstance(item, str):
            continue
        spec = item.strip()
        if spec and spec not in seen:
            dst.append(spec)
            seen.add(spec)


def load_permission_rules(cwd: Optional[Path] = None) -> Optional[PermissionConfig]:
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

    for path in settings_paths(cwd):
        perms = load_mote_json_section(path, "permissions", "settings")
        for key in _RULE_KEYS:
            _extend_unique(buckets[key], perms.get(key))

    if not (allow or deny or ask):
        return None
    return PermissionConfig(allow=allow, deny=deny, ask=ask)


__all__ = ["SETTINGS_FILE_NAME", "settings_paths", "load_permission_rules"]
