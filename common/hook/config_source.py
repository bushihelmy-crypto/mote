"""Global hooks config source — the user-level ``.mote/hooks.json`` file.

Global hook rules live in their *own* file, ``.mote/hooks.json``, discovered by
walking from the working directory up to the git root (plus a user-level
``~/.mote/hooks.json``) — NOT on :class:`RoleSchema.hooks`. This is deliberate,
mirroring :mod:`mote.executor.mcp.config_source`:

* **Global by construction.** These rules apply uniformly to *every* Role,
  loaded at app-level wiring, not baked into a single Role's schema (which stays
  for genuinely per-Role differences).
* **Ecosystem-familiar shape.** The top-level ``"hooks"`` key maps directly to
  :attr:`HookConfig.events` (event name → matcher groups), matching Claude
  Code's ``settings.json`` ``hooks`` key — a user can paste a rule block
  verbatim.
* **Per-project + user layering.** The ``<dir>/.mote/hooks.json`` walk mirrors
  MCP / skills: ``~/.mote/hooks.json`` is the lowest layer, closer-to-cwd files
  add more (see :func:`load_global_hooks`).

Everything is best-effort: a missing / empty / malformed file yields ``None``
(hooks simply stay unconfigured), never an exception into wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mote.common.const.paths import load_mote_json_section, mote_layered_files
from mote.common.logs import logger
from mote.common.schema.hook_config import HookConfig

#: The canonical global hooks config file name. Lives under ``~/.mote/`` and
#: under each project's ``.mote/`` dir.
HOOKS_CONFIG_FILE_NAME = "hooks.json"


def load_global_hooks(cwd: Optional[Path] = None) -> Optional[HookConfig]:
    """Merge every ``.mote/hooks.json`` on the layered walk into one HookConfig.

    Files are read low→high (``~/.mote/hooks.json`` then the git-root→cwd walk);
    for each event, matcher-group lists from every file are **concatenated**
    (order preserved) — global and closer rules all fire, and ``fold()`` resolves
    conflicts (deny > ask > allow). Best-effort throughout: a missing / empty /
    malformed file, or a config that fails validation, yields ``None`` rather
    than raising.
    """
    merged: dict[str, list] = {}
    for path in mote_layered_files(HOOKS_CONFIG_FILE_NAME, cwd):
        section = load_mote_json_section(path, "hooks", "Global hooks config")
        for event, groups in section.items():
            if isinstance(groups, list):
                merged.setdefault(event, []).extend(groups)  # concat across files
            else:
                logger.warning(f"Global hooks config: event '{event}' is not a list, skipping.")

    if not merged:
        return None
    try:
        return HookConfig(events=merged)  # pydantic validates raw dicts → groups
    except Exception as exc:  # noqa: BLE001 — a bad config must never break wiring
        logger.warning(f"Global hooks config invalid, ignoring: {exc}")
        return None


def merge_hook_configs(*cfgs: Optional[HookConfig]) -> Optional[HookConfig]:
    """Concat matcher-group lists per event across configs (order preserved).

    ``None`` configs are skipped; if nothing remains, returns ``None`` so the
    hook layer stays unbuilt. Both global and per-Role handlers fire — the
    :class:`HookManager` fold applies deny > ask > allow precedence, so
    concatenation is the correct merge semantics.
    """
    merged: dict[str, list] = {}
    for cfg in cfgs:
        if cfg is None:
            continue
        for event, groups in cfg.events.items():
            merged.setdefault(event, []).extend(groups)
    if not merged:
        return None
    return HookConfig(events=merged)


__all__ = ["HOOKS_CONFIG_FILE_NAME", "load_global_hooks", "merge_hook_configs"]
